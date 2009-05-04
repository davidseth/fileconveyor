import logging
import logging.handlers
import Queue
import os
import stat
import threading
import time
import signal
import sys
from UserList import UserList


sys.path.append(os.path.abspath('dependencies'))


from config import *
from persistent_queue import *
from persistent_list import *
from fsmonitor import *
from filter import *
from processors.processor import *
from transporters.transporter import *


LOG_FILE = './daemon.log'
PERSISTENT_DATA_FILE = './persistent_data.db'
WORKING_DIR = '/tmp/test'
MAX_FILES_IN_PIPELINE = 50
MAX_SIMULTANEOUS_PROCESSORCHAINS = 20
MAX_SIMULTANEOUS_TRANSPORTERS = 10


# Copied from django.utils.functional
def curry(_curried_func, *args, **kwargs):
    def _curried(*moreargs, **morekwargs):
        return _curried_func(*(args+moreargs), **dict(kwargs, **morekwargs))
    return _curried


class PeekingQueue(UserList):
    def peek(self):
        return self[0]

    def put(self, item):
        self.append(item)

    def get(self):
        return self.pop(0)

    def qsize(self):
        return len(self)


class Arbitrator(threading.Thread):
    """docstring for arbitrator"""


    def __init__(self, configfile="config.xml"):
        threading.Thread.__init__(self)
        self.lock = threading.Lock()
        self.die = False
        self.processorchains_running = 0
        self.transporters_running = 0

        # Set up logger.
        self.logger = logging.getLogger("Arbitrator")
        self.logger.setLevel(logging.DEBUG)
        # Handlers.
        fileHandler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5242880, backupCount=5)
        consoleHandler = logging.StreamHandler()
        consoleHandler.setLevel(logging.ERROR)
        # Formatters.
        formatter = logging.Formatter("%(asctime)s - %(name)-25s - %(levelname)-8s - %(message)s")
        fileHandler.setFormatter(formatter)
        consoleHandler.setFormatter(formatter)
        self.logger.addHandler(fileHandler)
        self.logger.addHandler(consoleHandler)
        self.logger.info("Arbitrator is initializing.")

        # Load config file.
        self.configfile = configfile
        self.logger.info("Loading config file.")
        self.config = Config("Arbitrator")
        self.config_errors = self.config.load(self.configfile)
        self.logger.info("Loaded config file.")
        if self.config_errors > 0:
            self.logger.error("Cannot continue, please fix the errors in the config file first.")
            return

        # TRICKY: set the "symlinkWithin" setting for "none" transporters
        # First calculate the value for the "symlinkWithin" setting.
        source_paths = []
        for (name, path) in self.config.sources.items():
            source_paths.append(path)
        symlinkWithin = ":".join(source_paths)
        # Then set it for every server that uses the "none" transporter.
        for name in self.config.servers.keys():
            if self.config.servers[name]["transporter"] == "none":
                self.config.servers[name]["settings"]["symlinkWithin"] = symlinkWithin


    def setup(self):
        self.processor_chain_factory = ProcessorChainFactory("Arbitrator", WORKING_DIR)

        # Create transporter (cfr. worker thread) pools for each server.
        # Create one initial transporter per pool, possible other transporters
        # will be created on-demand.
        self.transporters = {}
        for server in self.config.servers.keys():
            self.transporters[server] = []
            self.logger.info("Created transporter pool for the '%s' server." % (server))
            self.__create_transporter(server)

        # Create objects associated with each rule.
        self.rules = []
        self.logger.info("Creating objects associated with each rule.")
        for (name, path) in self.config.sources.items():
            if self.config.rules.has_key(name):
                root_path = self.config.sources[name]
                for rule in self.config.rules[name]:
                    prepend_root_path = lambda path: os.path.join(root_path, path)
                    paths = map(prepend_root_path, rule["filterConditions"]["paths"].split(":"))
                    rule["filterConditions"]["paths"] = ":".join(paths)
                    self.rules.append({
                        "source"         : name,
                        "label"          : rule["label"],
                        "filter"         : Filter(rule["filterConditions"]),
                        "processorChain" : rule["processorChain"],
                        "destination"    : rule["destination"],
                    })
                    self.logger.info("Created objects for rule '%s' for source '%s'." % (rule["label"], name))

        # Initialize the FSMonitor.
        fsmonitor_class = get_fsmonitor()
        self.logger.info("Using the %s FSMonitor class." % (fsmonitor_class))
        self.fsmonitor = fsmonitor_class(self.fsmonitor_callback, True)
        self.logger.info("Initialized FSMonitor.")

        # Initialize the persistent queues, thread-crossing queues and
        # persistent lists..
        # TRICKY: we don't use Python's shelve module because it's loaded into
        # memory in its entirety. In the case of a huge backlag of files that
        # still have to be filtered, processed or transported, say, 1 million
        # files, this would result in hundreds of megabytes of memory usage.
        # Persistent data.
        self.pipeline_queue = PersistentQueue("pipeline_queue", PERSISTENT_DATA_FILE)
        self.logger.info("Initialized 'pipeline' persistent queue, contains %d items." % (self.pipeline_queue.qsize()))
        self.files_in_pipeline =  PersistentList("pipeline_list", PERSISTENT_DATA_FILE)
        self.logger.info("Initialized 'files_in_pipeline' persistent list, contains %d items." % (len(self.files_in_pipeline)))
        # Move files from pipeline to pipeline queue. This is what prevents
        # files from being dropped from the pipeline!
        #for i in range(0, len(self.files_in_pipeline)):
        #    item = self.files_in_pipeline[]
        # Queues.
        self.discover_queue  = Queue.Queue()
        self.filter_queue    = Queue.Queue()
        self.process_queue   = Queue.Queue()
        self.transport_queue = {}
        for server in self.config.servers.keys():
            self.transport_queue[server] = PeekingQueue()
        self.db_queue        = Queue.Queue()
        self.logger.info("Initialized queues.")

        # Monitor all source paths.
        for (name, path) in self.config.sources.items():
            self.logger.info("Monitoring '%s' (%s)." % (path, name))
            self.fsmonitor.add_dir(path, FSMonitor.CREATED | FSMonitor.MODIFIED | FSMonitor.DELETED)


    def run(self):
        if self.config_errors > 0:
            return

        # Do all setup within the run() method to ensure all thread-bound
        # objects are created in the right thread.
        self.setup()

        # Start the FS monitor.
        self.fsmonitor.start()

        while not self.die:
            #self.logger.info("%d threads are running" % (threading.activeCount()))
            self.__process_discover_queue()
            self.__process_pipeline_queue()
            self.__process_filter_queue()
            self.__process_process_queue()
            self.__process_transport_queues()
            self.__process_db_queue()

            # Syncing the queues 10 times per second is more than sufficient,
            # because files are modified, processed and transported much
            # slower than that.
            time.sleep(0.1)

        self.logger.info("Stopping.")

        # Stop the FSMonitor and wait for its thread to end.
        self.fsmonitor.stop()
        self.fsmonitor.join()
        self.logger.info("Stopped FSMonitor.")

        # Stop the transporters and wait for their threads to end.
        for server in self.transporters.keys():
            for transporter in self.transporters[server]:
                transporter.stop()
                transporter.join()
            self.logger.info("Stopped transporters for the '%s' server." % (server))

        # Log information about the persistent data.
        self.logger.info("'pipeline' persistent queue contains %d items." % (self.pipeline_queue.qsize()))
        self.logger.info("'files_in_pipeline' persistent list contains %d items." % (len(self.files_in_pipeline)))


    def __process_discover_queue(self):
        self.lock.acquire()
        while self.discover_queue.qsize() > 0:
            (input_file, event) = self.discover_queue.get()
            self.pipeline_queue.put((input_file, event))
            self.logger.info("Syncing: added ('%s', %d) to the pipeline queue." % (input_file, event))
        self.lock.release()


    def __process_pipeline_queue(self):
        # As soon as there's room in the pipeline, move the file from the
        # pipeline queue into the pipeline.
        while self.pipeline_queue.qsize() > 0 and len(self.files_in_pipeline) < MAX_FILES_IN_PIPELINE:
            self.lock.acquire()

            # Peek the first item from the pipeline queue and store it in the
            # persistent 'files_in_pipeline' list so the data can never get
            # lost.
            self.files_in_pipeline.append(self.pipeline_queue.peek())

            # Pipeline queue -> filter queue.
            (input_file, event) = self.pipeline_queue.get()
            self.filter_queue.put((input_file, event))

            self.lock.release()
            self.logger.info("Pipelining: moved ('%s', %d) from the pipeline queue into the pipeline (into the filter queue)." % (input_file, event))
            


    def __process_filter_queue(self):
        # Process items in the 'filter' queue.
        while self.filter_queue.qsize() > 0:
            self.lock.acquire()
            (input_file, event) = self.filter_queue.get()
            self.lock.release()

            # Find all rules that apply to the detected file event.
            match_found = False
            for rule in self.rules:
                # Try to find a rule that matches the file.
                if rule["filter"].matches(input_file):
                    match_found = True
                    server     = rule["destination"]["server"]
                    self.logger.info("Filtering: '%s' matches the '%s' rule for the '%s' source!" % (input_file, rule["label"], rule["source"]))
                    # If the file was deleted, also delete the file on all
                    # servers.
                    self.lock.acquire()
                    if event == FSMonitor.DELETED:
                        if not rule["destination"] is None:
                            # TODO: set output_file equal to transported_file
                            # (which should be looked up in the DB)???
                            output_file = input_file
                            self.transport_queue[server].put((input_file, event, rule, output_file))
                            self.logger.info("Filtering: queued transporter to server '%s' for file '%s' to delete it ('%s' rule)." % (server, input_file, rule["label"]))
                    else:
                        # If a processor chain is configured, queue the file to
                        # be processed. Otherwise, immediately queue the file
                        # to be transported 
                        if not rule["processorChain"] is None:
                            self.process_queue.put((input_file, event, rule))
                            processor_chain_string = "->".join(rule["processorChain"])
                            self.logger.info("Filtering: queued processor chain '%s' for file '%s' ('%s' rule)." % (processor_chain_string, input_file, rule["label"]))
                        elif not rule["destination"] is None:
                            output_file = input_file
                            self.transport_queue[server].put((input_file, event, rule, output_file))
                            self.logger.info("Filtering: ueued transporter to server '%s' for file '%s' ('%s' rule)." % (server, input_file, rule["label"]))
                        else:
                            raise Exception("Either a processor chain or a destination must be defined.")
                    self.lock.release()

            # Log the lack of matches.
            if not match_found:
                self.logger.info("Filtering: '%s' matches no rules. Discarding this file." % (input_file))


    def __process_process_queue(self):
        while self.process_queue.qsize() > 0 and self.processorchains_running < MAX_SIMULTANEOUS_PROCESSORCHAINS:
            self.lock.acquire()
            (input_file, event, rule) = self.process_queue.get()
            self.lock.release()

            # Create a curried callback so we can pass additional data to the
            # processor chain callback without passing it to the processor
            # chain itself (which cannot handle sending additional data to its
            # callback function).
            curried_callback = curry(self.processor_chain_callback,
                                     event=event,
                                     rule=rule
                                     )

            # Start the processor chain.
            processor_chain = self.processor_chain_factory.make_chain_for(input_file, rule["processorChain"], curried_callback)
            processor_chain.start()
            self.processorchains_running += 1

            # Log.
            processor_chain_string = "->".join(rule["processorChain"])
            self.logger.info("Processing: started the '%s' processor chain for the file '%s'." % (processor_chain_string, input_file))


    def __process_transport_queues(self):
        # Process each server's transport queue.
        for server in self.config.servers.keys():
            while self.transport_queue[server].qsize() > 0:
                # Peek at the first item from the queue
                self.lock.acquire()
                (input_file, event, rule, output_file) = self.transport_queue[server].peek()
                self.lock.release()

                # Derive the action from the event.
                if event == FSMonitor.DELETED:
                    action = Transporter.DELETE
                else:
                    action = Transporter.ADD_MODIFY

                # Get the additional settings from the rule.
                parent_path = ""
                if rule["destination"]["settings"].has_key("path"):
                    parent_path = rule["destination"]["settings"]["path"]

                (id, transporter) = self.__get_transporter(server)
                if not transporter is None:
                    self.lock.acquire()
                    (input_file, event, rule, output_file) = self.transport_queue[server].get()
                    self.lock.release()

                    # Create a curried callback so we can pass additional data
                    # to the transporter callback without passing it to the
                    # transporter itself (which cannot handle sending
                    # additional data to its callback function).
                    curried_callback = curry(self.transporter_callback,
                                             event=event,
                                             input_file=input_file,
                                             rule=rule
                                             )

                    # Calculate src and dst for the file, then queue it to be
                    # transported.
                    src = output_file
                    dst = self.__calculate_transporter_dst(src, parent_path)
                    transporter.sync_file(src, dst, action, curried_callback)
                    self.logger.info("Transporting: queued '%s' to transfer to server '%s' with transporter #%d (of %d)." % (output_file, server, id + 1, len(self.transporters[server])))
                else:
                    self.logger.info("Transporting: no more transporters are available for server '%s'." % (server))
                    break


    def __process_db_queue(self):
        # Process the db queue.
        while self.db_queue.qsize() > 0:
            self.lock.acquire()
            (input_file, event, rule, output_file, transported_file, url) = self.db_queue.get()
            self.lock.release()

            # TODO
            print "Finalizing: storing in DB:", (input_file, event, rule, output_file, transported_file, url)
            
            self.lock.acquire()
            self.files_in_pipeline.remove((input_file, event))
            self.lock.release()
            print "Removed from index:", (input_file, event)


    def __get_transporter(self, server):
        # Try to find a running transporter that is ready for new work.
        for id in range(0, len(self.transporters[server])):
            transporter = self.transporters[server][id]
            if transporter.is_ready():
                return (id, transporter)

        # Since we didn't find any ready transporter, check if we can create
        # a new one.
        # Don't run more than the allowed number of simultaneous transporters.
        if not self.transporters_running < MAX_SIMULTANEOUS_TRANSPORTERS:
            return None
        if self.config.servers[server]["maxConnections"] < len(self.transporters[server]):
            id          = len(self.transporters[server]) - 1
            transporter = self.__create_transporter(server)
            return (id, transporter)
        else:
            return None


    def __create_transporter(self, server):
        transporter_name = self.config.servers[server]["transporter"]
        settings = self.config.servers[server]["settings"]

        # Determine which class to import.
        transporter_modulename = "transporters.transporter_" + transporter_name
        _temp = __import__(transporter_modulename, globals(), locals(), ["TRANSPORTER_CLASS"], -1)
        transporter_classname = _temp.TRANSPORTER_CLASS

        # Get a reference to that class.
        module = __import__(transporter_modulename, globals(), locals(), [transporter_classname])
        transporter_class = getattr(module, transporter_classname)

        # Create an instance of the transporter and add it to the pool.
        transporter = transporter_class(settings, self.transporter_callback)
        transporter.start()
        self.transporters[server].append(transporter)

        self.transporters_running += 1
        self.logger.info("Created '%s' transporter for the '%s' server." % (transporter_name, server))

        return transporter


    def __calculate_transporter_dst(self, src, parent_path=None):
        dst = src

        # Strip off the working directory.
        if dst.startswith(WORKING_DIR):
            dst = dst[len(WORKING_DIR):]

        # Prepend any possible parent path.
        if not parent_path is None:
            dst = os.path.join(parent_path, dst)

        return dst


    def fsmonitor_callback(self, monitored_path, event_path, event):
        print "FSMONITOR CALLBACK FIRED:\n\tmonitored_path='%s'\n\tevent_path='%s'\n\tevent=%d" % (monitored_path, event_path, event)
        # Ignore directories.
        if not stat.S_ISDIR(os.stat(event_path)[stat.ST_MODE]):
            input_file = event_path

            # Add to discover queue.
            self.lock.acquire()
            self.discover_queue.put((input_file, event))
            self.lock.release()


    def processor_chain_callback(self, input_file, output_file, event, rule):
        print "PROCESSOR CHAIN CALLBACK FIRED\n\tinput_file='%s'\n\toutput_file='%s'" % (input_file, output_file)

        # Decrease number of running processor chains.
        self.lock.acquire()
        self.processorchains_running -= 1
        self.lock.release()

        # If a destination is defined, then add it to the transport queue.
        # Otherwise, do nothing.
        if not rule["destination"] is None:
            # We need to know to which server this file should be transported to
            # in order to know in which queue to put the file.
            server = rule["destination"]["server"]

            # Add to transport queue.
            self.lock.acquire()
            self.transport_queue[server].put((input_file, event, rule, output_file))
            self.lock.release()


    def transporter_callback(self, src, dst, url, action, input_file, event, rule):
        print "TRANSPORTER CALLBACK FIRED:\n\tsrc='%s'\n\tdst='%s'\n\turl='%s'\n\taction=%d\n\t(curried): event=%d\n\t(curried): input_file='%s'" % (src, dst, url, action, event, input_file)

        output_file      = src
        transported_file = dst

        # Add to db queue.
        self.lock.acquire()
        self.db_queue.put((input_file, event, rule, output_file, transported_file, url))
        self.lock.release()


    def stop(self):
        self.logger.info("Signaling to stop.")
        self.lock.acquire()
        self.die = True
        self.lock.release()


if __name__ == '__main__':
    arbitrator = Arbitrator("config.sample.xml")
    arbitrator.start()
    if arbitrator.isAlive():
        time.sleep(30)
        arbitrator.stop()
        arbitrator.join()
    

    # def handleKeyboardInterrupt(signalNumber, frame):
    #     print "stopping"
    #     arbitrator.stop()
    #     print "stopped!"
    # 
    # try :
    #     # Register a signal handler for ctrl-C, control-z
    #     signal.signal(signal.SIGINT, handleKeyboardInterrupt)
    #     signal.signal(signal.SIGTSTP, handleKeyboardInterrupt)
    # 
    #     # Signal doesn't work.
    #     arbitrator.start()
    # 
    #     # Signal works (because no separate thread is started).
    #     #arbitrator.run()
    # finally:
    #     pass