#!/usr/bin/env python3
""" Listens and exposes events of docker to Zabbix

Author - Denis Ashcheulov
"""
import json
import logging
import logging.config
import os
import subprocess
import sys
import time
import re
from argparse import ArgumentParser
from queue import Queue
from signal import signal
from threading import Thread

import docker
import yaml
from docker.errors import APIError

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


def zabbix_send(item, value):
    """Send a zabbix trap

    :type item: str
    :param item: zabbix item name
    :type value: str, Exception
    :param value: value to send
    """
    args = ["zabbix_sender", "-k", "%s" % item, "-o", "%s" % value]
    zabbix_conf = "/etc/zabbix/zabbix_agentd.conf"
    args.extend(["-c", zabbix_conf])
    logger.debug("Executing '%s'", ' '.join(args))
    return subprocess.call(args, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


class GracefulKiller(object):  # pylint: disable=too-few-public-methods
    """Catch signals to allow graceful shutdown."""

    def __init__(self):
        self.received_signal = self.received_term_signal = False
        self.last_signal = None
        caught_signals = [1, 2, 3, 10, 12, 15]
        for signum in caught_signals:
            signal(signum, self.handler)

    def handler(self, signum, frame):  # pylint: disable=unused-argument,missing-docstring
        self.last_signal = signum
        self.received_signal = True
        if signum in [2, 3, 15]:
            self.received_term_signal = True


class EventListener(Thread):
    """
    A thread that listen events of docker and puts to queue
    """

    def __init__(self, client, queue, _filter=None):
        Thread.__init__(self, name='EventListener')
        self.daemon = True
        self.stop = False
        self._client = client
        self._queue = queue
        self.filter = _filter or (lambda x: x)

        # Automatically start thread
        self.start()

    def run(self):
        logger.info('Starting docker event listener.')
        failures = 0

        while not self.stop:
            try:
                event = next(self._client.events(decode=True))
                logger.debug('Caught event:\n---\n%s...', yaml.safe_dump(event, default_style='yaml'))
                filtered_event = self.filter(event)
                logger.debug('Filter has thrown it off') if not filtered_event else logger.debug('Putting to the queue')
                self._queue.put(filtered_event)
                # Reset failure count on successful read from the API.
                failures = 0
            except (APIError, StopIteration) as error:
                # If we encounter a failure, wait ten seconds before retrying and
                # mark the failures. After three consecutive failures, we'll
                # stop the thread.
                failures += 1
                if failures > 3:
                    logger.exception('Unable to read events')
                    self.stop = True
                else:
                    logger.warning('Unable to read events. %s', error)
                time.sleep(10)
        logger.info('Stopped docker event listener.')


class Worker(Thread):
    """
    A thread that process messages from a queue
    """

    def __init__(self, config, queue):
        Thread.__init__(self, name='Worker')
        self.stop = False
        self._queue = queue
        self._config = config
        self.containers = config.pre_discovered_containers

        # Automatically start thread
        self.start()

    @property
    def zabbix_discovery(self):
        """ :returns prepared discovery for zabbix"""
        return json.dumps({'data': [{'{#HCONTAINERID}': c,
                                     '{#IS_NOT_RUNNING_TS}': self._config.is_not_running_thresholds.get(
                                         c,
                                         self._config.is_not_running_thresholds.get('default', '1w'))}
                                    for c
                                    in self.containers]})

    def run(self):
        logger.info('Starting worker thread.')
        failures = 0
        if zabbix_send('docker.discovery', self.zabbix_discovery):
            logger.error('Failed to send key \'docker.discovery\' to zabbix')
        else:
            logger.info('Sent zabbix discovery for pre-discovered containers %s', ', '.join(self.containers))

        while not self.stop:
            event = self._queue.get()
            if not event:
                self._queue.task_done()
                continue
            try:
                container_name = event['Actor']['Attributes'].get('name')
                if event['Type'] == 'container' and self._config.discovery_new_containers and \
                        container_name not in self.containers and \
                        not any(map(lambda x: re.match(x, container_name), self._config.blacklisted_containers)):
                    logger.info('Discovered new container %s. Trapping to zabbix', container_name)
                    self.containers.append(container_name)
                    zabbix_send('docker.discovery', self.zabbix_discovery)
                    time.sleep(self._config.sleep_after_discovery_seconds)
                self.process(event)
                failures = 0

            except Exception as error:  # pylint: disable=broad-except
                failures += 1
                if failures > 2:
                    logger.exception('Failed 3 times. Skipping event %s', event)
                    self.stop = True
                else:
                    logger.warning('%s', error)
                    self._queue.put(event)
                time.sleep(10)
            finally:
                self._queue.task_done()
        logger.info('Stopped worker thread.')

    def process(self, event):
        """ Contain logic of processing event"""

        name = event['Actor']['Attributes'].get('name')
        action = event.get('Action')
        if action == 'die':
            exitcode = event['Actor']['Attributes']['exitCode']
            logger.info('Container %s has died with exit code %s', name, exitcode)
            self.trap(name, 'docker.exit_status[/%s]' % name, exitcode)
        elif 'health_status' in action:
            if ' unhealthy' in action:
                logger.info('Container %s is unhealthy', name)
                self.trap(name, 'docker.health_status[/%s]' % name, 0)
            elif ' healthy' in action:
                logger.info('Container %s is healthy', name)
                self.trap(name, 'docker.health_status[/%s]' % name, 1)
        elif action == 'start':
            logger.info('Container %s started', name)
        elif action == 'kill':
            logger.info('Signal %s was sent to container %s', event['Actor']['Attributes'].get('signal'), name)
        elif action == 'stop':
            logger.info('Container %s stopped at %s', name,
                        time.strftime("%b %d %Y %H:%M:%S", time.localtime(event['time'])))

    def trap(self, container_name, key, value):
        """ Send event to zabbix trapper if it's allowed """

        if container_name in self.containers:
            logger.info('Sending zabbix trapper %s=%s', key, value)
            if zabbix_send(key, value):
                logger.warning('Could not trap %s', key)
                if self._config.zabbix_item_trap_err:
                    zabbix_send(self._config.zabbix_item_trap_err, 1)
        elif any(map(lambda x: re.match(x, container_name), self._config.blacklisted_containers)):
            logger.info('Container %s is in blacklist. Skipping trapping to zabbix', container_name)
        else:
            logger.info('Container %s is undiscovered. Skipping trapping to zabbix', container_name)


class App(object):
    """Main class of application"""

    def __init__(self):
        class Settings:  # pylint: disable=too-few-public-methods,no-init,old-style-class
            """ Contains default settings which may be overwritten in config file or cmd-arguments"""
            log_level = 'info'
            filter = None
            zabbix_item_trap_err = None
            keep_alive_item_key = None
            keep_alive_interval = 300
            docker_base_url = 'unix://var/run/docker.sock'
            min_docker_api_version = '1.21'
            pre_discovered_containers = list()
            discovery_new_containers = True
            blacklisted_containers = list()
            sleep_after_discovery_seconds = 15  # zabbix cannot process discovered items immediately
            is_not_running_thresholds = {'default': '1w'}
            logging = yaml.load('''
                version: 1
                disable_existing_loggers: False
                formatters:
                    console:
                        format: '%(asctime)s %(levelname)s: %(message)s'
                        datefmt: '%Y-%m-%d %H:%M:%S'
                handlers:
                    console:
                        class: logging.StreamHandler
                        formatter: console
                        stream: 'ext://sys.stdout'
                root:
                    handlers: [console]
                    level: INFO
            ''')

        self.config = Settings()
        self.load_settings()
        self.config.logging['root']['level'] = self.config.log_level.upper()
        logging.config.dictConfig(self.config.logging)
        self.client = docker.APIClient(base_url=self.config.docker_base_url, version=self.config.min_docker_api_version)
        self.queue = Queue()

    def load_settings(self):
        """ Loads setting from config_file if it is provided as cmd-argument of from cmd-arguments
        Settings are defined in the next order: defaults, config file, cmd-arguments.
        It means that defaults may be overwritten by config, which may be overwritten by cmd-args.
        """
        parser = ArgumentParser()
        parser.add_argument("--{}".format('config_file'), default=getattr(self.config, 'config_file', 0))
        parser.parse_known_args(args=[a for a in sys.argv[1:] if a not in ['-h', '--help']], namespace=self.config)
        if os.path.isfile(self.config.config_file):
            logger.info('Reading config file %s', self.config.config_file)
            self.config.__dict__.update(yaml.safe_load(open(self.config.config_file)))
        for item in (k for k in dir(self.config) if not k.startswith('__')):
            if getattr(self.config, item) is True:
                parser.add_argument("--no-{}".format(item), dest=item, action='store_false',
                                    default=getattr(self.config, item))
            elif getattr(self.config, item) is False:
                parser.add_argument("--{}".format(item), action='store_true', default=getattr(self.config, item))
            elif item != 'config_file':
                parser.add_argument("--{}".format(item), default=getattr(self.config, item))
        parser.parse_args(namespace=self.config)
        if not os.path.isfile(self.config.config_file):
            delattr(self.config, 'config_file')

    def create_thread(self, _type):
        """ Factory method for creation threads
        :param _type type of a thread
        :returns instance of a thread
        """
        return _type(*(lambda _type:
                       [self.client, self.queue, Filters().get(self.config.filter)]
                       if _type == EventListener else [self.config, self.queue])(_type))

    def run(self):
        """ Entry point of the script"""
        logger.debug(
            'Started with settings:\n---\n%s...', yaml.safe_dump(self.config.__dict__, default_style='yaml'))
        killer = GracefulKiller()
        threads = [self.create_thread(EventListener), self.create_thread(Worker)]
        time_counter = 0
        while not killer.received_term_signal:
            if killer.received_signal:
                logger.info("Ignoring signal %s", killer.last_signal)
                killer.received_signal = False
            for thread in threads:
                if not thread.is_alive():
                    threads.remove(thread)
                    logger.error('%s has been stopped. Will try to restart in 60 s', thread.name)
                    if self.config.keep_alive_item_key:
                        zabbix_send(self.config.keep_alive_item_key, 0)
                    time.sleep(60)
                    threads.append(self.create_thread(type(thread)))
            if time_counter >= self.config.keep_alive_interval:
                time_counter = 0
                if self.config.keep_alive_item_key:
                    zabbix_send(self.config.keep_alive_item_key, 1)
            time.sleep(0.2)
            time_counter += 0.2
        logger.info("Received signal %s. Gracefully exiting.", killer.last_signal)
        self.queue.join()
        for thread in threads:
            thread.stop = True
        self.queue.put(None)

    @classmethod
    def entry(cls):
        return cls().run()


class Filters(object):  # pylint: disable=no-init
    """ Set of filters """

    def get(self, name):
        """ Method get
        :returns callable filter by its name"""
        if name:
            try:
                _filter = [getattr(self, f) for f in dir(self) if f == name][0]
            except IndexError:
                logger.warning('There is no filter %s', name)
            else:
                return _filter

    @staticmethod
    def only_containers(event):
        """ :returns events tied with containers"""
        return event if event['Type'] == 'container' else None

    @staticmethod
    def containers_without_exec(event):
        """ :returns events tied with containers without actions of exec"""
        return event if event['Type'] == 'container' and "exec" not in event['Action'] else None
