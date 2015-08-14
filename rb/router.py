from weakref import ref as weakref
from binascii import crc32

from rb.ketama import Ketama
from rb._rediscommands import COMMANDS


class UnroutableCommand(Exception):
    pass


class BaseRouter(object):

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

    def get_host_for_command(self, command, args):
        """Returns the host this command should be executed against."""
        return self.get_host_for_key(self.get_key(command, args))

    def get_host_for_key(self, key):
        """Perform routing and return host_id of the target."""
        raise NotImplementedError()


class ConsistentHashingRouter(BaseRouter):
    """Router that returns the host_id based on a consistent hashing
    algorithm.  The consistent hashing algorithm only works if a key
    argument is provided.
    """

    def __init__(self, cluster):
        BaseRouter.__init__(self, cluster)
        self._host_id_id_map = dict(self.cluster.hosts.items())
        self._hash = Ketama(self._host_id_id_map.values())

    def get_host_for_key(self, key):
        rv = self._hash.get_node(key)
        if rv is None:
            raise UnroutableCommand('Did not find a suitable host for the key.')
        return rv


class PartitionRouter(BaseRouter):
    """A straightforward router that just individually routes commands to
    single nodes based on a simple crc32 % node_count setup.
    """

    def get_host_for_key(self, key):
        if isinstance(key, unicode):
            k = key.encode('utf-8')
        else:
            k = str(key)
        return crc32(k) % len(self.cluster.hosts)
