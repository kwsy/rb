import time

from weakref import ref as weakref
from binascii import crc32
from threading import Lock

from abanico.ketama import Ketama
from abanico._rediscommands import COMMANDS


class UnableToSetupRouter(Exception):
    pass


class UnroutableCommand(Exception):
    pass


class BaseRouter(object):

    retryable = False

    def __init__(self, cluster=None):
        # this is a weakref because the router is cached on the cluster
        # and otherwise we end up in circular reference land and we are
        # having problems being garbage collected.
        self._cluster = weakref(cluster)

    @property
    def cluster(self):
        rv = self._cluster()
        if rv is None:
            raise RuntimeError('Cluster went away')
        return rv

    def get_key(self, command, args):
        """Returns the key a command operates on."""
        key_positions = COMMANDS.get(command.upper(), Ellipsis)

        if key_positions is Ellipsis:
            raise UnroutableCommand('The command "%r" is unknown to the '
                                    'router and cannot be handled as a '
                                    'result.' % command)
        elif key_positions is not None:
            # There is no key in the command
            if not key_positions:
                return None

            # A single key was sent
            elif len(key_positions) == 1:
                try:
                    return args[key_positions[0]]
                except LookupError:
                    return None

        raise UnroutableCommand(
            'The command "%r" operates on multiple keys which is '
            'something that is not supported.' % command)

    def get_host(self, command, args):
        """Returns the host this command should be executed against."""
        args = self.pre_routing(command=command, args=args)
        host_id = self.route(command=command, args=args)
        return self.post_routing(command=command, args=args,
                                 host_id=host_id)

    def pre_routing(self, command, args):
        """Perform any prerouting with this method and return the args.
        """
        return args

    def route(self, command, args):
        """Perform routing and return host_id of the target."""
        raise NotImplementedError()

    def post_routing(self, command, host_id, args):
        """Perform any postrouting actions and return host_id."""
        return host_id


class ConsistentHashingRouter(BaseRouter):
    """Router that returns the host_id based on a consistent hashing
    algorithm.  The consistent hashing algorithm only works if a key
    argument is provided.
    """

    # XXX: this code really needs some sanity checking.  It already seemed
    # questionable in nydus

    # If this router can be retried on if a particular db index it gave out did
    # not work
    retryable = True

    # Number of seconds a host must be marked down before it is elligable to be
    # put back in the pool and retried.
    retry_timeout = 30

    def __init__(self, cluster):
        BaseRouter.__init__(self, cluster)
        self._host_id_id_map = {}
        self._down_connections = {}
        self._host_id_id_map = dict(self.cluster.hosts.items())
        self._hash_lock = Lock()
        self._hash = Ketama(self._host_id_id_map.values())

    def check_down_connections(self):
        now = time.time()
        for host_id, marked_down_at in self._down_connections.items():
            if marked_down_at + self.retry_timeout <= now:
                self.mark_connection_up(host_id)

    def mark_connection_down(self, host_id):
        self._down_connections[host_id] = time.time()
        with self._hash_lock:
            self._hash.remove_node(self._host_id_id_map[host_id])

    def mark_connection_up(self, host_id):
        self._down_connections.pop(host_id, None)
        with self._hash_lock:
            self._hash.add_node(self._host_id_id_map[host_id])

    def pre_routing(self, command, args):
        self.check_down_connections()
        return BaseRouter.pre_routing(self, command, args)

    def route(self, command, args):
        key = self.get_key(command, args)
        with self._hash_lock:
            return self._hash.get_node(key)

    def post_routing(self, command, host_id, args):
        if host_id is not None and host_id in self._down_connections:
            self.mark_connection_up(host_id)
        return host_id


class PartitionRouter(BaseRouter):
    """A straightforward router that just individually routes commands to
    single nodes based on a simple crc32 % node_count setup.
    """

    def route(self, command, args):
        key = self.get_key(command, args)
        if isinstance(key, unicode):
            k = key.encode('utf-8')
        else:
            k = str(key)
        return crc32(k) % len(self.cluster.hosts)
