import random
import string
import socket
import signal

import tornado.ioloop
import tornado.web
import tornado.auth

from cloud.aws import CloudHost
import db
from db import JBoxDynConfig, JBoxUserV2, is_cluster_leader, is_proposed_cluster_leader, JBoxDBPlugin
from jbox_tasks import JBoxAsyncJob
from jbox_util import LoggerMixin, JBoxCfg
from jbox_tasks import JBoxHousekeepingPlugin
from vol import VolMgr, JBoxVol
from jbox_container import JBoxContainer
from handlers import AdminHandler, MainHandler, PingHandler, CorsHandler
from handlers import JBoxHandlerPlugin, JBoxUIModulePlugin


class JBox(LoggerMixin):
    shutdown = False

    def __init__(self):
        LoggerMixin.configure()
        db.configure()
        CloudHost.configure()
        JBoxContainer.configure()
        VolMgr.configure()

        JBoxAsyncJob.configure()
        JBoxAsyncJob.init(JBoxAsyncJob.MODE_PUB)

        self.application = tornado.web.Application(handlers=[
            (r"/", MainHandler),
            (r"/hostadmin/", AdminHandler),
            (r"/ping/", PingHandler),
            (r"/cors/", CorsHandler)
        ])
        JBoxHandlerPlugin.add_plugin_handlers(self.application)
        JBoxUIModulePlugin.create_include_files()

        # cookie_secret = ''.join(random.choice(string.ascii_uppercase + string.digits) for x in xrange(32))
        # use sesskey as cookie secret to be able to span multiple tornado servers
        self.application.settings["cookie_secret"] = JBoxCfg.get('sesskey')
        self.application.settings["plugin_features"] = JBox._get_pluggedin_features()
        self.application.listen(JBoxCfg.get('port'), address=socket.gethostname())
        self.application.listen(JBoxCfg.get('port'), address='localhost')

        self.ioloop = tornado.ioloop.IOLoop.instance()

        # run container maintainence every 5 minutes
        run_interval = 5 * 60 * 1000
        self.log_info("Container maintenance every " + str(run_interval / (60 * 1000)) + " minutes")
        self.ct = tornado.ioloop.PeriodicCallback(JBox.do_housekeeping, run_interval, self.ioloop)
        self.sigct = tornado.ioloop.PeriodicCallback(JBox.do_signals, 1000, self.ioloop)

    @staticmethod
    def _get_pluggedin_features():
        feature_providers = dict()
        for pluginclass in [JBoxHousekeepingPlugin, JBoxDBPlugin, JBoxHandlerPlugin, JBoxUIModulePlugin, JBoxVol]:
            for plugin in pluginclass.plugins:
                for feature in plugin.provides:
                    if feature in feature_providers:
                        feature_providers[feature].append(plugin)
                    else:
                        feature_providers[feature] = [plugin]
        return feature_providers

    def run(self):
        if CloudHost.ENABLED['route53']:
            try:
                CloudHost.deregister_instance_dns()
                JBox.log_warn("Prior dns registration was found for the instance")
            except:
                JBox.log_debug("No prior dns registration found for the instance")
            CloudHost.register_instance_dns()
        JBoxContainer.publish_container_stats()
        JBox.do_update_user_home_image()
        JBoxAsyncJob.async_refresh_disks()

        JBox.log_debug("Setting up signal handlers")
        signal.signal(signal.SIGINT, JBox.signal_handler)
        signal.signal(signal.SIGTERM, JBox.signal_handler)

        JBox.log_debug("Starting ioloops")
        self.ct.start()
        self.sigct.start()
        self.ioloop.start()
        JBox.log_info("Stopped.")

    @staticmethod
    def do_update_user_home_image():
        if VolMgr.has_update_for_user_home_image():
            if not VolMgr.update_user_home_image(fetch=False):
                JBoxAsyncJob.async_update_user_home_image()

    @staticmethod
    def monitor_registrations():
        max_rate = JBoxDynConfig.get_registration_hourly_rate(CloudHost.INSTALL_ID)
        rate = JBoxUserV2.count_created(1)
        reg_allowed = JBoxDynConfig.get_allow_registration(CloudHost.INSTALL_ID)
        JBox.log_debug("registration allowed: %r, rate: %d, max allowed: %d", reg_allowed, rate, max_rate)

        if (reg_allowed and (rate > max_rate*1.1)) or ((not reg_allowed) and (rate < max_rate*0.9)):
            reg_allowed = not reg_allowed
            JBox.log_warn("Changing registration allowed to %r", reg_allowed)
            JBoxDynConfig.set_allow_registration(CloudHost.INSTALL_ID, reg_allowed)

        if reg_allowed:
            num_pending_activations = JBoxUserV2.count_pending_activations()
            if num_pending_activations > 0:
                JBox.log_info("scheduling activations for %d pending activations", num_pending_activations)
                JBoxAsyncJob.async_schedule_activations()

    @staticmethod
    def is_ready_to_terminate():
        if not JBoxCfg.get('cloud_host.scale_down'):
            return False

        num_sessions = JBoxContainer.num_sessions()
        return (num_sessions == 0) and CloudHost.can_terminate(is_proposed_cluster_leader())

    @staticmethod
    def do_housekeeping():
        terminating = False
        server_delete_timeout = JBoxCfg.get('expire')
        JBoxContainer.maintain(max_timeout=server_delete_timeout, inactive_timeout=JBoxCfg.get('inactivity_timeout'),
                               protected_names=JBoxCfg.get('protected_docknames'))
        is_leader = is_cluster_leader()
        if is_leader:
            JBox.log_info("I am the cluster leader")
            JBox.monitor_registrations()
            if not JBoxDynConfig.is_stat_collected_within(CloudHost.INSTALL_ID, 1):
                JBoxAsyncJob.async_collect_stats()
        elif JBox.is_ready_to_terminate():
            terminating = True
            JBox.log_warn("terminating to scale down")
            try:
                CloudHost.deregister_instance_dns()
            except:
                JBox.log_error("Error deregistering instance dns")
            CloudHost.terminate_instance()

        if not terminating:
            JBox.do_update_user_home_image()
            JBoxAsyncJob.async_plugin_maintenance(is_leader)

    @staticmethod
    def signal_handler(signum, frame):
        JBox.shutdown = True
        JBox.log_info("Received signal %r", signum)

    @staticmethod
    def do_signals():
        if JBox.shutdown:
            JBox.log_info("Shutting down...")
            tornado.ioloop.IOLoop.instance().stop()