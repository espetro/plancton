# -*- coding: utf-8 -*-
import docker, json, pprint, requests, yaml
import base64, string, time, os, random, errno
from functools import wraps
from yaml import YAMLError
from socket import gethostname
import logging, logging.handlers
from prettytable import PrettyTable
from datetime import datetime
from daemon import Daemon
from docker import Client
import docker.errors
import sys
from influxdb_streamer import InfluxDBStreamer

def apparmor_enabled():
  try:
    return open("/sys/module/apparmor/parameters/enabled").read().strip() == "Y"
  except IOError:
    return False

def mem_size():
  return int(os.sysconf('SC_PAGE_SIZE')*os.sysconf('SC_PHYS_PAGES'))

def swap_size():
  return [ int(x.split(":")[1].strip().split(" ")[0])*1024 for x in open("/proc/meminfo").read().split("\n") if x.startswith("SwapTotal") ][0]

def cpu_count():
  return int(os.sysconf("SC_NPROCESSORS_ONLN"))

def cpu_times():
  return [ float(x) for x in open('/proc/uptime').read().split(' ') ]

def utc_time():
  return time.mktime(datetime.utcnow().timetuple())

# Wrap API calls and catch exceptions to provide "robustness"
def robust(tries=5, delay=3, backoff=2):
  def robust_decorator(f):
    @wraps(f)
    def robust_call(self, *args, **kwargs):
      ltries, ldelay = tries, delay
      while ltries > 1:
        try:
          return f(self, *args, **kwargs)
        except requests.exceptions.ConnectionError as e:
          self.logctl.warning("In %s: cannot connect to Docker, retrying in %d s: %s" % \
                              (f.__name__, ldelay, e))
          self.streamer(series="daemon",
                        tags={ "hostname": self._hostname },
                        fields={ "status": "waiting" })
          time.sleep(ldelay)
          ltries -= 1
          ldelay *= backoff
        except requests.exceptions.ReadTimeout as e: # Unresponsive docker daemon
          self.logctl.warning("In %s: Docker timed out, retrying in %d s: %s" % \
                              (f.__name__, ldelay, e))
          time.sleep(ldelay)
          ltries -= 1
          ldelay *= backoff
        except docker.errors.DockerException as e:
          self.logctl.warning("In %s: API request failed, retrying in %d seconds: %s" % \
                              (f.__name__, ldelay, e))
          self.streamer(series="daemon",
                        tags={ "hostname": self._hostname },
                        fields={ "status": "waiting" })
          time.sleep(ldelay)
          ltries -= 1
          ldelay *= backoff
      raise Exception("In %s: call failed after %d attempts, giving up" % (f.__name__, tries))
    return robust_call
  return robust_decorator

class Lazy():
  def __init__(self, init_func):
    self.content = None
    self.init_func = init_func
  def __call__(self):
    if not self.content:
      self.content = self.init_func()
    return self.content

class Plancton(Daemon):
  __version__ = '0.6.0'
  @robust()
  def container_list(self, all=True):
    return self.docker_client().containers(all=all)
  @robust()
  def container_remove(self, id, force):
    return self.docker_client().remove_container(container=id, force=force)
  @robust()
  def docker_pull(self, repository, tag="latest"):
    self.logctl.debug("Pulling: repo %s tag %s" % (repository, tag))
    return self.docker_client().pull(repository=repository, tag=tag)
  @robust()
  def container_create_from_conf(self, jsonconf, name):
    return self.docker_client().create_container_from_config(config=jsonconf, name=name)
  @robust()
  def container_inspect(self, id):
    return self.docker_client().inspect_container(container=id)
  @robust()
  def container_start(self, id):
    return self.docker_client().start(container=id)
  @property
  def idle(self):
   return float(100 - self.efficiency)

  # Set daemon name, pidfile, log directory and location of docker socket.
  def __init__(self, name, pidfile, logdir, rundir, confdir,
               socket_url="unix://var/run/docker.sock"):
    super(Plancton, self).__init__(name, pidfile)
    self._start_time = self._last_update_time = self._last_confup_time = time.time()
    self._last_kill_time = 0
    self._overhead_first_time = 0
    self.uptime0,self.idletime0 = cpu_times()
    self._logdir = logdir
    self._rundir = rundir
    self._confdir = confdir
    self._sockpath = socket_url
    self._num_cpus = cpu_count()
    self._hostname = gethostname().split('.')[0]
    self._cont_config = None  # container configuration (dict)
    self._container_prefix = "plancton-worker"
    self._drainfile = self._rundir + "/drain"
    self._drainfile_stop = self._rundir + "/stop"
    self._fstopfile = self._rundir + "/force-stop"
    self._force_kill = False
    self._do_main_loop = True
    self._has_image = False
    self.streamers = set()
    self.docker_client = Lazy(lambda: Client(base_url=self._sockpath, version="auto"))
    self.conf = {
      "influxdb_url"      : set(),            # URL set to InfluxDB (with #database)
      "updateconfig"      : 60,               # frequency of config updates (s)
      "image_expiration"  : 43200,            # frequency of image updates (s)
      "main_sleep"        : 30,               # main loop sleep (s)
      "grace_kill"        : 120,              # kill after secs over CPU threshold
      "grace_spawn"       : 60,               # spawn secs after last kill
      "cpus_per_dock"     : 1,                # number of CPUs per container (frac)
      "max_docks"         : "ncpus - 2",      # expression: compute max containers
      "docks_per_loop"    : 4,                # max docks launched each loop
      "max_ttl"           : 43200,            # max ttl for a container (12 hours)
      "docker_image"      : "busybox",        # Docker image: repository[:tag]
      "docker_cmd"        : "/bin/sleep 60",  # command to run (string or list)
      "docker_privileged" : False,            # run container privileged
      "max_dock_mem"      : 2000000000,       # maximum RAM per container (bytes)
      "max_dock_swap"     : 0,                # maximum swap per container (bytes)
      "user_group"        : "0:0",            # run container as uid:gid
      "binds"             : [],               # list of bind mounts (all read-only)
      "devices"           : [],               # list of exposed devices
      "capabilities"      : [],               # list of added caps (e.g. SYS_ADMIN)
      "security_opts"     : []                # sec options (e.g. apparmor profile)
    }

  # Get only own running containers, youngest container first if reverse=True.
  def _filtered_list(self, name, reverse=True):
    self.logctl.debug("Fetching list of Plancton running containers")
    try:
      jlist = self.container_list(all=True)
    except Exception as e:
      self.logctl.error("Couldn't get containers list, returning empty: %s" % e)
      return []
    fjlist = [ d for d in jlist if (d.get("Names", [""])[0][1:].startswith(name) and d["Status"].startswith("Up")) ]
    srtlist = sorted(fjlist, key=lambda k: k["Created"], reverse=reverse)
    return srtlist

  # Set up rotating logging system. Logfiles in: `self._logdir`.
  def _setup_log_files(self):
    if not os.path.isdir(self._logdir):
      os.mkdir(self._logdir, 0700)
    else:
      os.chmod(self._logdir, 0700)
    format = '%(asctime)s %(name)s %(levelname)s [%(module)s.%(funcName)s] %(message)s'
    datefmt = '%Y-%m-%d %H:%M:%S'
    log_file_handler = logging.handlers.RotatingFileHandler(self._logdir + '/plancton.log',
      mode='a', maxBytes=10000000, backupCount=50)
    log_file_handler.setFormatter(logging.Formatter(format, datefmt))
    log_file_handler.doRollover()
    self.logctl.setLevel(logging.DEBUG)
    self.logctl.addHandler(log_file_handler)

  # Parse configuration file `config.yaml` and change default value if specified.
  def _read_conf(self):
    try:
      conf = yaml.safe_load(open(self._confdir+"/config.yaml").read())
    except (IOError, YAMLError) as e:
      self.logctl.error("%s/config.yaml could not be read, using previous one: %s" % (self._confdir, e))
      conf = {}
    if conf is None:
      conf = {}
    for k in self.conf:
      self.conf[k] = conf.get(k, self.conf[k])
    try:
      self.conf["max_docks"] = int(eval(str(self.conf["max_docks"]),
                                        { "ram_bytes": mem_size(),
                                          "swap_bytes": swap_size(),
                                          "ncpus": cpu_count(),
                                          "max_dock_mem": conf["max_dock_mem"],
                                          "max_dock_swap": conf["max_dock_swap"] }))
    except Exception as e:
      self.logctl.error("configuration for max_docks is invalid, falling back to zero: %s: %s" % \
                        (self.conf["max_docks"], e))
      self.conf["max_docks"] = 0
    if not isinstance(self.conf["docker_cmd"], list):
      self.conf["docker_cmd"] = self.conf["docker_cmd"].split(" ")
    if isinstance(self.conf["influxdb_url"], str):
      self.conf["influxdb_url"] = set([self.conf["influxdb_url"]])
    elif isinstance(self.conf["influxdb_url"], list):
      self.conf["influxdb_url"] = set(filter(lambda x: "#" in x, self.conf["influxdb_url"]))
    else:
      self.conf["influxdb_url"] = set()
    self.logctl.debug("Configuration:\n%s" % json.dumps(self.conf, indent=2, default=list))

  # Set up monitoring target.
  def _influxdb_setup(self):
    self.streamers = set([ x for x in self.streamers if x.baseurl+"#"+x.database in self.conf["influxdb_url"] ])
    for url in self.conf["influxdb_url"]:
      self.streamers.add(InfluxDBStreamer(**dict(zip(["baseurl", "database"], url.split("#", 1)))))

  # Efficiency is calculated subtracting idletime per cpu from uptime.
  def _set_cpu_efficiency(self):
    curruptime,curridletime = cpu_times()
    deltaup = curruptime - self.uptime0
    deltaidle = curridletime - self.idletime0
    eff = float(100)
    try:
      eff = float((deltaup*self._num_cpus - deltaidle)*100) / float(deltaup*self._num_cpus)
    except ZeroDivisionError as e:
      pass
    self.uptime0 = curruptime
    self.idletime0 = curridletime
    self.efficiency = eff if eff > 0 else 0.0

  # Kill running containers exceeding a given CPU threshold.
  def _overhead_control(self):
    max_containers_cpu = 100 * self.conf["cpus_per_dock"] * min(self._count_containers(), self.conf["max_docks"]) / cpu_count()
    if max_containers_cpu and self.efficiency > max_containers_cpu+10.:
      if self._overhead_first_time == 0:
        self._overhead_first_time = time.time()
      now = time.time()
      self.logctl.warning("Above CPU threshold of %.2f%% for %d/%d s" % (max_containers_cpu, now-self._overhead_first_time, self.conf["grace_kill"]))
      if now-self._overhead_first_time > self.conf["grace_kill"]:
        cont_list = self._filtered_list(name=self._container_prefix)
        if cont_list:
          self.logctl.debug("Killing container %s" % cont_list[0]["Id"])
          try:
            self.container_remove(cont_list[0]["Id"], force=True)
            self._last_kill_time = time.time()
          except Exception as e:
            self.logctl.error("Cannot remove %s: %s", cont_list[0]["Id"], e)
          else:
            self.logctl.info('Container %s removed successfully' % cont_list[0]["Id"])
        else:
          self.logctl.debug('No workers found, nothing to do')
          self._overhead_first_time = 0
    else:
      self._overhead_first_time = 0

  # Create a container. Returns the container ID on success, None otherwise.
  def _create_container(self):
    uuid = ''.join(random.SystemRandom().choice(string.digits + string.ascii_lowercase) for _ in range(6))
    cname = self._container_prefix + '-' + uuid
    c = { "Cmd"        : self.conf["docker_cmd"],
          "Image"      : self.conf["docker_image"],
          "Hostname"   : "plancton-%s-%s" % (self._hostname[:40], uuid),
          "User"       : self.conf["user_group"],
          "HostConfig" : { "CpuQuota"    : int(self.conf["cpus_per_dock"]*100000.),
                           "CpuPeriod"   : 100000,
                           "NetworkMode" : "bridge",
                           "SecurityOpt" : self.conf["security_opts"] if apparmor_enabled() else [],
                           "Binds"       : [ x+":rw,shared,Z" for x in self.conf["binds"] ],
                           "Memory"      : self.conf["max_dock_mem"],
                           "MemorySwap"  : self.conf["max_dock_mem"] + self.conf["max_dock_swap"],
                           "Privileged"  : self.conf["docker_privileged"],
                           "Devices"     : [ dict(zip([ "PathOnHost", "PathInContainer",
                                                        "CgroupPermissions" ], x.split(":", 2)))
                                             for x in self.conf["devices"] ],
                           "CapAdd"      : [ x.lstrip("+") for x in self.conf["capabilities"] if x and x[0]!="-" ],
                           "CapDrop"     : [ x.lstrip("-") for x in self.conf["capabilities"] if x and x[0]=="-" ]
                         }
        }
    self.logctl.debug("Container definition for %s:\n%s" % (cname, json.dumps(c, indent=2)))
    try:
      return self.container_create_from_conf(jsonconf=c, name=cname)
    except Exception as e:
      self.logctl.error("Cannot create container: %s", e)
      return None

  # Start a created container. Return PID it if the container is actually running.
  def _start_container(self, container):
    if container is None:
      return None
    self.logctl.debug("Starting %s" % str(container["Id"]))
    try:
      self.container_start(id=container["Id"])
    except Exception as e:
      self.logctl.error(e)
      return None
    try:
      jj = self.container_inspect(id=container["Id"])
      pid = int(jj["State"]["Pid"])
      if pid:
        return pid
    except Exception as e:
      self.logctl.error(e)
    return None

  # Pretty print the statuses of controlled containers.
  def _dump_container_list(self):
    status_table = PrettyTable(['n\'', 'docker hash', 'status', 'docker name', '   pid   '])
    status_table.align["   pid   "] = "r"
    try:
      clist = self.container_list(all=True)
    except Exception as e:
      self.logctl.error("Couldn't get container list: %s", e)
      return
    num = 0
    for c in clist:
      if c.get("Names", [""])[0][1:].startswith(self._container_prefix):
        num = num+1
        shortid = c['Id'][:12]
        status = c['Status'].lower()
        name = c['Names'][0][1:]
        try:
          pid = self.container_inspect(id=c['Id'])['State'].get('Pid', 0)
        except Exception as e:
          self.logctl.warning("While inspecting container %s: %s" % (shortid, e))
          continue
        pid = "" if pid == 0 else str(pid)
        status_table.add_row([num, shortid, status, name, pid])
    self.logctl.info('Container list:\n' + str(status_table))

  # Return the number of running containers.
  def _count_containers(self):
    try:
      clist = self.container_list(all=False)
    except Exception as e:
      self.logctl.error("Couldn't get containers list, defaulting running value to zero: %s", e)
      return 0
    return len([ x for x in clist if x.get("Status", "").startswith("Up")
                   and x.get("Names", [""])[0][1:].startswith(self._container_prefix) ])

  # Clean up dead or stale containers.
  def _control_containers(self):
    try:
      clist = self.container_list(all=True)
    except Exception as e:
      self.logctl.error("Couldn't get containers list: %s", e)
      return
    for i in clist:
      if not i.get("Names", [""])[0][1:].startswith(self._container_prefix):
        self.logctl.debug("Ignoring container %s", i.get("Names", [""])[0][1:])
        continue
      to_remove = False
      # TTL threshold block
      if "running" in i['State']:
        try:
          insdata = self.container_inspect(i["Id"])
        except Exception as e:
          self.logctl.error("Couldn't get container information! %s", e)
        else:
          statobj = datetime.strptime(insdata['State']['StartedAt'][:19], "%Y-%m-%dT%H:%M:%S")
          dock_uptime = utc_time() - time.mktime(statobj.timetuple())
          if dock_uptime > self.conf["max_ttl"] or self._force_kill:
            if self._force_kill:
              self.logctl.info("Force killing %s" if self._force_kill else \
                               "Killing %s since it exceeded the max TTL", i['Id'])
            for streamer in self.streamers:
              streamer(series="container",
                       tags={ "hostname": self._hostname,
                              "started": True,
                              "killed": True },
                       fields={ "uptime": dock_uptime })
            to_remove = True
          else:
            self.logctl.debug("Container %s is below its maximum TTL, leaving it alone", i["Id"])
      else:
        # Bad status block
        self.logctl.info("Killing non-running container %s (status is %s)", i["Id"], i["Status"])
        if "exited" in i["State"]:
          # Container has terminated
          try:
            insdata = self.container_inspect(i["Id"])
          except Exception as e:
            self.logctl.error("Couldn't get container information! %s", e)
          else:
            statobj_start = datetime.strptime(insdata['State']['StartedAt'][:19], "%Y-%m-%dT%H:%M:%S")
            statobj_end = datetime.strptime(insdata['State']['FinishedAt'][:19], "%Y-%m-%dT%H:%M:%S")
            dock_uptime = time.mktime(statobj_end.timetuple()) - time.mktime(statobj_start.timetuple())
            for streamer in self.streamers:
              streamer(series="container",
                       tags={ "hostname": self._hostname,
                              "started": True,
                              "killed": False },
                       fields={"uptime": dock_uptime})
        if "created" in i["State"]:
          # Container has never had any chance to start :-(
          for streamer in self.streamers:
            streamer(series="container",
                     tags={ "hostname": self._hostname,
                            "started": False,
                            "killed": False },
                     fields={ "uptime": 0 })
        to_remove = True

      if to_remove:
        try:
          self.container_remove(id=i['Id'], force=True)
        except Exception as e:
          self.logctl.warning('It was not possible to remove container with id %s: %s', i['Id'], e)
        else:
          self.logctl.info("Removed container %s", i["Id"])
        self._last_kill_time = time.time()
    if self._force_kill:
      self._force_kill = False
      try:
        os.remove(self._fstopfile)
      except OSError as e:
        if e.errno != errno.ENOENT:
          self.logctl.error("Cannot remove force-stop status file %s: %s" % (self._fstopfile, e))

  def drain(self, stop=False):
    try:
      os.open(self._drainfile, os.O_CREAT|os.O_EXCL, 0644)
      if stop:
        self.logctl.info("Drain-stop mode requested, no new containers started, will exit afterwards")
        os.open(self._drainfile_stop, os.O_CREAT|os.O_EXCL, 0644)
      else:
        self.logctl.info("Drain mode requested, no new containers started")
    except OSError as e:
      if e.errno != errno.EEXIST:
        self.logctl.error("Cannot create drain/drain-stop status file(s): %s" % e)
        return False
    return True

  def resume(self):
    self.logctl.info("Exiting drain mode: new containers will be started")
    try:
      os.remove(self._drainfile)
    except OSError as e:
      if e.errno != errno.ENOENT:
        self.logctl.warning("Cannot remove drain status file %s: %s" % (self._drainfile, e))

  def kill(self):
    self.logctl.info("Force-stop mode requested: not starting new containers and killing running ones")
    try:
      os.open(self._fstopfile, os.O_CREAT|os.O_EXCL, 0644)
    except OSError as e:
      if e.errno != errno.EEXIST:
        self.logctl.error("Cannot create force-stop status file %s: %s" % (self._fstopfile, e))
        return False
    return True

  def onexit(self):
    self.logctl.info("Graceful termination requested: will exit gracefully soon")
    self._do_main_loop = False

  def init(self):
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("docker").setLevel(logging.WARNING)
    self._setup_log_files()
    self.logctl.info("---- plancton v%s running with pid %d ----" % (self.__version__, os.getpid()))
    try:
      os.remove(self._fstopfile)
    except OSError as e:
      pass
    if not os.path.isdir(self._rundir):
      os.mkdir(self._rundir, 0700)
    else:
      os.chmod(self._rundir, 0700)
    self._read_conf()
    self._influxdb_setup()
    try:
      self.docker_pull(*self.conf["docker_image"].split(":", 1))
      self._has_image = True
    except Exception as e:
      self.logctl.error("Cannot pull Docker image %s during init: will retry later" % self.conf["docker_image"])
    self._control_containers()

  # Main loop, do comparison between uptime and thresholds sets for updates.
  def main_loop(self):
    self._set_cpu_efficiency()
    now = time.time()
    for streamer in self.streamers:
      streamer(series="daemon",
               tags={ "hostname": self._hostname },
               fields={ "uptime": now - self._start_time })
    delta_config = now - self._last_confup_time
    delta_update = now - self._last_update_time
    draining = os.path.isfile(self._drainfile)
    if draining:
      self.logctl.info("Drain status file %s found: no new containers will be started" % self._drainfile)
    if self._force_kill:
      self.logctl.info("Force kill file %s found: not starting containers, killing existing" % self._fstopfile)
    self._overhead_control()
    prev_img = self.conf["docker_image"]
    prev_influxdb_url = self.conf["influxdb_url"]
    if delta_config >= int(self.conf["updateconfig"]):
      self._read_conf()
      self._last_confup_time = time.time()
    if not self._has_image or prev_img != self.conf["docker_image"] or delta_update >= int(self.conf["image_expiration"]):
      self._has_image = False
      try:
        self.docker_pull(*self.conf["docker_image"].split(":", 1))
        self._has_image = True
        self._last_update_time = time.time()
      except Exception as e:
        self.logctl.error("Cannot pull Docker image %s: no new containers, will retry later" % \
                          self.conf["docker_image"])
    if prev_influxdb_url.symmetric_difference(self.conf["influxdb_url"]):
      self._influxdb_setup()
    running = self._count_containers()
    self.logctl.debug("CPU used: %.2f%%, available: %.2f%%" % (self.efficiency, self.idle))
    for streamer in self.streamers:
      streamer(series="measurement",
               tags={ "hostname": self._hostname },
               fields={ "cpu_eff": self.efficiency })
      streamer(series="daemon",
               tags={ "hostname": self._hostname },
               fields={ "containers": running,
                        "status": "draining" if draining else "active" })
    fitting_docks = int(self.idle*0.95*self._num_cpus/(self.conf["cpus_per_dock"]*100))
    launchable_containers = min(fitting_docks,
                                max(self.conf["max_docks"]-running, 0),
                                self.conf["docks_per_loop"])
    self.logctl.debug("Potentially fitting containers based on CPU utilisation: %d", fitting_docks)
    if self._has_image and not draining and not self._force_kill:
      if now-self._last_kill_time > self.conf["grace_spawn"]:
        self.logctl.info("Will launch %d new container(s)" % launchable_containers)
        for _ in range(launchable_containers):
          if not self._start_container(self._create_container()):
            self.logctl.warning("Starting container failed: not attempting to launch other containers this time")
            break
      elif launchable_containers > 0:
        self.logctl.info("Not launching %d containers: too little time since last kill" % \
                         launchable_containers)
    self._control_containers()
    self._last_update_time = time.time()
    self._dump_container_list()
    if running == 0 and draining and os.path.isfile(self._drainfile_stop):
      self.logctl.info("Drain-stop requested. No running containers found, will exit.")
      os.remove(self._drainfile_stop)
      self.onexit()


  # Main daemon function. Return is in the range 0-255.
  def run(self):
    self.init()
    while self._do_main_loop or self._force_kill:
      count = 0
      self.main_loop()
      self.logctl.debug("Sleeping %d seconds..." % self.conf["main_sleep"])
      self._force_kill = os.path.isfile(self._fstopfile)
      while self._do_main_loop and count < self.conf["main_sleep"] and not self._force_kill:
        time.sleep(1)
        count = count+1
        self._force_kill = os.path.isfile(self._fstopfile)
    self.logctl.info("Exiting gracefully")
    return 0
