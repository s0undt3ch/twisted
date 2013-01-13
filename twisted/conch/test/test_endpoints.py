# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Tests for L{twisted.conch.endpoints}.
"""

from zope.interface.verify import verifyObject
from zope.interface import implementer, providedBy

from twisted.python.filepath import FilePath
from twisted.python.failure import Failure
from twisted.trial.unittest import TestCase
from twisted.internet.interfaces import IStreamClientEndpoint
from twisted.internet.protocol import Factory, Protocol
from twisted.internet.defer import Deferred, succeed
from twisted.internet.error import ConnectionDone, ConnectionRefusedError
from twisted.internet.address import IPv4Address

from twisted.cred.portal import Portal
from twisted.cred.checkers import InMemoryUsernamePasswordDatabaseDontUse

from twisted.conch.interfaces import IConchUser
from twisted.conch.error import ConchError, UserRejectedKey, HostKeyChanged
from twisted.conch.ssh.factory import SSHFactory
from twisted.conch.ssh.userauth import SSHUserAuthServer
from twisted.conch.ssh.connection import SSHConnection
from twisted.conch.ssh.keys import Key
from twisted.conch.ssh.channel import SSHChannel
from twisted.conch.client.knownhosts import KnownHostsFile

from twisted.internet.task import Clock
from twisted.test.proto_helpers import StringTransport, MemoryReactor
from twisted.test.iosim import FakeTransport, connect
from twisted.conch.test.keydata import (
    publicRSA_openssh, privateRSA_openssh, privateDSA_openssh)
from twisted.conch.avatar import ConchUser

from twisted.conch.endpoints import AuthenticationFailed, SSHCommandAddress, SSHCommandEndpoint


class BrokenExecSession(SSHChannel):
    def request_exec(self, data):
        return 0



class WorkingExecSession(SSHChannel):
    def request_exec(self, data):
        return 1



class TrivialRealm(object):
    def __init__(self):
        self.channelLookup = {}

    def requestAvatar(self, avatarId, mind, *interfaces):
        avatar = ConchUser()
        avatar.channelLookup = self.channelLookup
        return (IConchUser, avatar, lambda: None)



class AddressSpyFactory(Factory):
    address = None

    def buildProtocol(self, address):
        self.address = address
        return Factory.buildProtocol(self, address)



class FixedResponseUI(object):
    def __init__(self, result):
        self.result = result


    def prompt(self, text):
        return succeed(self.result)


    def warn(self, text):
        pass



class FakeClockSSHUserAuthServer(SSHUserAuthServer):
    # Simplify the tests by disconnecting after the first authentication
    # failure.  One should attempt should be sufficient to test authentication
    # success and failure.  There is an off-by-one in the implementation of
    # this feature in Conch, so set it to 0 in order to allow 1 attempt.
    attemptsBeforeDisconnect = 0

    @property
    def clock(self):
        """
        Use the reactor defined by the factory, rather than the default global
        reactor, to simplify testing (by allowing an alternate implementation
        to be supplied by tests).
        """
        return self.transport.factory.reactor



class CommandFactory(SSHFactory):
    publicKeys = {
        'ssh-rsa': Key.fromString(data=publicRSA_openssh)
    }
    privateKeys = {
        'ssh-rsa': Key.fromString(data=privateRSA_openssh)
    }
    services = {
        'ssh-userauth': FakeClockSSHUserAuthServer,
        'ssh-connection': SSHConnection
    }

# factory = _SSHSecureConnectionFactory()
# secure = factory.getSecureConnection()
# authenticated = secure.getAuthenticatedConnection()
# command = authenticated.getCommandChannel()
# transport = command.getTransport()
# clientProtocol.makeConnection(transport)


@implementer(IStreamClientEndpoint)
class SpyClientEndpoint(object):
    def connect(self, factory):
        self.result = Deferred()
        self.factory = factory
        return self.result



class Composite(object):
    """
    A helper to compose other objects based on their declared (zope.interface)
    interfaces.

    This is used here to create a reactor from separate implementations of
    different reactor interfaces - for example, from L{Clock} and
    L{ReactorFDSet} to create a reactor which provides L{IReactorTime} and
    L{IReactorFDSet}.
    """
    def __init__(self, parts):
        """
        @param parts: An iterable of the objects to compose.  The methods of
            these objects which are part of any interface the objects declare
            they provide will be made methods of C{self}.  (Non-method
            attributes are not supported.)

        @raise ValueError: If an interface is provided by more than one of the
            objects in C{parts}.
        """
        seen = set()
        for p in parts:
            for i in providedBy(p):
                if i in seen:
                    raise ValueError("More than one part provides %r" % (i,))
                seen.add(i)
                for m in i.names():
                    setattr(self, m, getattr(p, m))



class SSHCommandEndpointTests(TestCase):
    """
    Tests for L{SSHCommandEndpoint}, an L{IStreamClientEndpoint}
    implementations which connects a protocol with the stdin and stdout of a
    command running in an SSH session.
    """
    def setUp(self):
        """
        Configure an SSH server with password authentication enabled for a
        well-known (to the tests) account.
        """
        self.hostname = b"ssh.example.com"
        self.port = 42022
        self.user = b"user"
        self.password = b"password"
        self.clock = Clock()
        self.memory = MemoryReactor()
        self.reactor = Composite([self.clock, self.memory])
        self.realm = TrivialRealm()
        self.portal = Portal(self.realm)
        self.passwdDB = InMemoryUsernamePasswordDatabaseDontUse()
        self.passwdDB.addUser(self.user, self.password)
        self.portal.registerChecker(self.passwdDB)
        self.factory = CommandFactory()
        self.factory.reactor = self.reactor
        self.factory.portal = self.portal
        self.factory.doStart()
        self.addCleanup(self.factory.doStop)

        self.clientAddress = IPv4Address("TCP", "10.0.0.1", 12345)
        self.serverAddress = IPv4Address("TCP", "192.168.100.200", 54321)

        # Make the server's host key available to be verified by the client.
        self.hostKeyPath = FilePath(self.mktemp())
        self.knownHosts = KnownHostsFile(self.hostKeyPath)
        self.knownHosts.addHostKey(
            self.hostname, self.factory.publicKeys['ssh-rsa'])
        self.knownHosts.addHostKey(
            self.serverAddress.host, self.factory.publicKeys['ssh-rsa'])
        self.knownHosts.save()


    def connectedServerAndClient(self, serverFactory, clientFactory):
        clientProtocol = clientFactory.buildProtocol(None)
        serverProtocol = serverFactory.buildProtocol(None)

        clientTransport = FakeTransport(
            clientProtocol, isServer=False, hostAddress=self.clientAddress,
            peerAddress=self.serverAddress)
        serverTransport = FakeTransport(
            serverProtocol, isServer=True, hostAddress=self.serverAddress,
            peerAddress=self.clientAddress)

        pump = connect(
            serverProtocol, serverTransport, clientProtocol, clientTransport)
        return serverProtocol, clientProtocol, pump


    def test_interface(self):
        """
        L{SSHCommandEndpoint} instances provide L{IStreamClientEndpoint}.
        """
        endpoint = SSHCommandEndpoint(
            self.reactor, self.hostname, self.port,
            b"dummy command", b"dummy user")
        self.assertTrue(verifyObject(IStreamClientEndpoint, endpoint))


    def test_destination(self):
        """
        L{SSHCommandEndpoint} uses the L{IReactorTCP} passed to it to attempt a
        connection to the host/port address also passed to it.
        """
        endpoint = SSHCommandEndpoint(
            self.reactor, self.hostname, self.port, b"/bin/ls -l",
            b"dummy user", knownHosts=self.knownHosts,
            ui=FixedResponseUI(False))
        factory = Factory()
        factory.protocol = Protocol
        endpoint.connect(factory)

        host, port, factory, timeout, bindAddress = self.memory.tcpClients[0]
        self.assertEqual(self.hostname, host)
        self.assertEqual(self.port, port)
        self.assertEqual(1, len(self.memory.tcpClients))


    def test_connectionFailed(self):
        """
        If a connection cannot be established, the L{Deferred} returned by
        L{SSHCommandEndpoint.connect} fires with a L{Failure} the reason for
        the connection setup failure.
        """
        endpoint = SSHCommandEndpoint(
            self.reactor, self.hostname, self.port, b"/bin/ls -l",
            b"dummy user", knownHosts=self.knownHosts,
            ui=FixedResponseUI(False))
        factory = Factory()
        factory.protocol = Protocol
        d = endpoint.connect(factory)

        factory = self.memory.tcpClients[0][2]
        factory.clientConnectionFailed(None, Failure(ConnectionRefusedError()))

        self.failureResultOf(d).trap(ConnectionRefusedError)


    def test_userRejectedHostKey(self):
        """
        If the L{KnownHostsFile} instance used to construct
        L{SSHCommandEndpoint} rejects the SSH public key presented by the
        server, the L{Deferred} returned by L{SSHCommandEndpoint.connect} fires
        with a L{Failure} wrapping L{UserRejectedKey}.
        """
        endpoint = SSHCommandEndpoint(
            self.reactor, self.hostname, self.port, b"/bin/ls -l",
            b"dummy user", knownHosts=KnownHostsFile(self.mktemp()),
            ui=FixedResponseUI(False))

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.connectedServerAndClient(
            self.factory, self.memory.tcpClients[0][2])

        f = self.failureResultOf(connected)
        f.trap(UserRejectedKey)


    def test_mismatchedHostKey(self):
        """
        If the SSH public key presented by the SSH server does not match the
        previously remembered key, as reported by the L{KnownHostsFile}
        instance use to construct the endpoint, for that server, the
        L{Deferred} returned by L{SSHCommandEndpoint.connect} fires with a
        L{Failure} wrapping L{HostKeyChanged}.
        """
        differentKey = Key.fromString(privateDSA_openssh).public()
        knownHosts = KnownHostsFile(self.mktemp())
        knownHosts.addHostKey(self.serverAddress.host, differentKey)
        knownHosts.addHostKey(self.hostname, differentKey)

        # The UI may answer true to any questions asked of it; they should
        # make no difference, since a *mismatched* key is not even optionally
        # allowed to complete a connection.
        ui = FixedResponseUI(True)

        endpoint = SSHCommandEndpoint(
            self.reactor, self.hostname, self.port, b"/bin/ls -l",
            b"dummy user", b"dummy password", knownHosts=knownHosts, ui=ui)

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.connectedServerAndClient(
            self.factory, self.memory.tcpClients[0][2])

        f = self.failureResultOf(connected)
        f.trap(HostKeyChanged)


    def test_connectionClosedBeforeSecure(self):
        """
        If the connection closes at any point before the SSH transport layer
        has finished key exchange (ie, gotten to the point where we may attempt
        to authenticate), the L{Deferred} returned by
        L{SSHCommandEndpoint.connect} fires with a L{Failure} wrapping the
        reason for the lost connection.
        """
        endpoint = SSHCommandEndpoint(
            self.reactor, self.hostname, self.port, b"/bin/ls -l",
            b"dummy user", knownHosts=self.knownHosts,
            ui=FixedResponseUI(False))

        factory = Factory()
        factory.protocol = Protocol
        d = endpoint.connect(factory)

        transport = StringTransport()
        factory = self.memory.tcpClients[0][2]
        client= factory.buildProtocol(None)
        client.makeConnection(transport)

        client.connectionLost(Failure(ConnectionDone()))
        self.failureResultOf(d).trap(ConnectionDone)


    def test_authenticationFailure(self):
        """
        If the SSH server rejects the credentials presented during
        authentication, the L{Deferred} returned by
        L{SSHCommandEndpoint.connect} fires with a L{Failure} wrapping
        L{AuthenticationFailed}.
        """
        endpoint = SSHCommandEndpoint(
            self.reactor, self.hostname, self.port, b"/bin/ls -l",
            b"dummy user",  b"dummy password", knownHosts=self.knownHosts,
            ui=FixedResponseUI(False))

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.connectedServerAndClient(
            self.factory, self.memory.tcpClients[0][2])

        # For security, the server delays password authentication failure
        # response.  Advance the simulation clock so the client sees the
        # failure.
        self.clock.advance(server.service.passwordDelay)

        # Let the failure response traverse the "network"
        pump.flush()

        f = self.failureResultOf(connected)
        f.trap(AuthenticationFailed)
        # XXX Should assert something specific about the arguments of the
        # exception

        # Nothing useful can be done with the connection at this point, so the
        # endpoint should close it.
        self.assertTrue(client.transport.disconnecting)


    def test_channelOpenFailure(self):
        """
        If a channel cannot be opened on the authenticated SSH connection, the
        L{Deferred} returned by L{SSHCommandEndpoint.connect} fires with a
        L{Failure} wrapping the reason given by the server.
        """
        endpoint = SSHCommandEndpoint(
            self.reactor, self.hostname, self.port, b"/bin/ls -l", self.user,
            password=self.password, knownHosts=self.knownHosts,
            ui=FixedResponseUI(False))

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.connectedServerAndClient(
            self.factory, self.memory.tcpClients[0][2])

        # The server logs the channel open failure - this is expected.
        errors = self.flushLoggedErrors(ConchError)
        self.assertIn(
            'unknown channel', (errors[0].value.data, errors[0].value.value))
        self.assertEqual(1, len(errors))

        # Now deal with the results on the endpoint side.
        f = self.failureResultOf(connected)
        f.trap(ConchError)
        self.assertEqual('unknown channel', f.value.value)

        # Nothing useful can be done with the connection at this point, so the
        # endpoint should close it.
        self.assertTrue(client.transport.disconnecting)


    def test_execFailure(self):
        """
        If execution of the command fails, the L{Deferred} returned by
        L{SSHCommandEndpoint.connect} fires with a L{Failure} wrapping the
        reason given by the server.
        """
        self.realm.channelLookup[b'session'] = BrokenExecSession
        endpoint = SSHCommandEndpoint(
            self.reactor, self.hostname, self.port, b"/bin/ls -l", self.user,
            password=self.password, knownHosts=self.knownHosts,
            ui=FixedResponseUI(False))

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.connectedServerAndClient(
            self.factory, self.memory.tcpClients[0][2])

        f = self.failureResultOf(connected)
        f.trap(ConchError)
        self.assertEqual('channel request failed', f.value.value)

        # Nothing useful can be done with the connection at this point, so the
        # endpoint should close it.
        self.assertTrue(client.transport.disconnecting)


    def test_buildProtocol(self):
        """
        Once the necessary SSH actions have completed successfully,
        L{SSHCommandEndpoint.connect} uses the factory passed to it to
        construct a protocol instance by calling its C{buildProtocol} method
        with an address object representing the SSH connection and command
        executed.
        """
        self.realm.channelLookup[b'session'] = WorkingExecSession
        endpoint = SSHCommandEndpoint(
            self.reactor, self.hostname, self.port, b"/bin/ls -l", self.user,
            password=self.password, knownHosts=self.knownHosts,
            ui=FixedResponseUI(False))

        factory = AddressSpyFactory()
        factory.protocol = Protocol
        endpoint.connect(factory)

        server, client, pump = self.connectedServerAndClient(
            self.factory, self.memory.tcpClients[0][2])

        self.assertIsInstance(factory.address, SSHCommandAddress)
        self.assertEqual(server.transport.getHost(), factory.address.server)
        self.assertEqual(self.user, factory.address.username)
        self.assertEqual(b"/bin/ls -l", factory.address.command)


    def test_makeConnection(self):
        """
        L{SSHCommandEndpoint} establishes an SSH connection, creates a channel
        in it, runs a command in that channel, and uses the protocol's
        C{makeConnection} to associate it with a protocol representing that
        command's stdin and stdout.
        """
        self.realm.channelLookup[b'session'] = WorkingExecSession
        endpoint = SSHCommandEndpoint(
            self.reactor, self.hostname, self.port, b"/bin/ls -l", self.user,
            password=self.password, knownHosts=self.knownHosts,
            ui=FixedResponseUI(False))

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.connectedServerAndClient(
            self.factory, self.memory.tcpClients[0][2])

        protocol = self.successResultOf(connected)
        self.assertNotIdentical(None, protocol.transport)


    def test_dataReceived(self):
        """
        After establishing the connection, when the command on the SSH server
        produces output, it is delivered to the protocol's C{dataReceived}
        method.
        """
        self.realm.channelLookup[b'session'] = WorkingExecSession
        endpoint = SSHCommandEndpoint(
            self.reactor, self.hostname, self.port, b"/bin/ls -l", self.user,
            password=self.password, knownHosts=self.knownHosts,
            ui=FixedResponseUI(False))

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.connectedServerAndClient(
            self.factory, self.memory.tcpClients[0][2])

        protocol = self.successResultOf(connected)
        dataReceived = []
        protocol.dataReceived = dataReceived.append

        server.service.channels[0].write(b"hello, world")
        pump.pump()
        self.assertEqual(b"hello, world", b"".join(dataReceived))


    def test_connectionLost(self):
        """
        When the command closes the channel, the protocol's C{connectionLost}
        method is called.
        """
        self.realm.channelLookup[b'session'] = WorkingExecSession
        endpoint = SSHCommandEndpoint(
            self.reactor, self.hostname, self.port, b"/bin/ls -l", self.user,
            password=self.password, knownHosts=self.knownHosts,
            ui=FixedResponseUI(False))

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.connectedServerAndClient(
            self.factory, self.memory.tcpClients[0][2])

        protocol = self.successResultOf(connected)
        connectionLost = []
        protocol.connectionLost= connectionLost.append

        server.service.channels[0].loseConnection()
        pump.pump()
        connectionLost[0].trap(ConnectionDone)

        # Nothing useful can be done with the connection at this point, so the
        # endpoint should close it.
        self.assertTrue(client.transport.disconnecting)


    def test_write(self):
        """
        The transport connected to the protocol has a C{write} method which
        sends bytes to the input of the command executing on the SSH server.
        """
        self.realm.channelLookup[b'session'] = WorkingExecSession
        endpoint = SSHCommandEndpoint(
            self.reactor, self.hostname, self.port, b"/bin/ls -l", self.user,
            password=self.password, knownHosts=self.knownHosts,
            ui=FixedResponseUI(False))

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.connectedServerAndClient(
            self.factory, self.memory.tcpClients[0][2])

        protocol = self.successResultOf(connected)

        dataReceived = []
        server.service.channels[0].dataReceived = dataReceived.append
        protocol.transport.write(b"hello, world")
        pump.pump()
        self.assertEqual(b"hello, world", b"".join(dataReceived))


    def test_writeSequence(self):
        """
        The transport connected to the protocol has a C{writeSequence} method which
        sends bytes to the input of the command executing on the SSH server.
        """
        self.realm.channelLookup[b'session'] = WorkingExecSession
        endpoint = SSHCommandEndpoint(
            self.reactor, self.hostname, self.port, b"/bin/ls -l", self.user,
            password=self.password, knownHosts=self.knownHosts,
            ui=FixedResponseUI(False))

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.connectedServerAndClient(
            self.factory, self.memory.tcpClients[0][2])

        protocol = self.successResultOf(connected)

        dataReceived = []
        server.service.channels[0].dataReceived = dataReceived.append
        protocol.transport.writeSequence(list(b"hello, world"))
        pump.pump()
        self.assertEqual(b"hello, world", b"".join(dataReceived))


    def test_loseConnection(self):
        """
        The transport connected to the protocol has a C{loseConnection} method
        which causes the channel in which the command is running to close and
        the overall connection to be closed.
        """
        self.realm.channelLookup[b'session'] = WorkingExecSession
        endpoint = SSHCommandEndpoint(
            self.reactor, self.hostname, self.port, b"/bin/ls -l", self.user,
            password=self.password, knownHosts=self.knownHosts,
            ui=FixedResponseUI(False))

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.connectedServerAndClient(
            self.factory, self.memory.tcpClients[0][2])

        protocol = self.successResultOf(connected)
        closed = []
        server.service.channels[0].closed = lambda: closed.append(True)
        protocol.transport.loseConnection()
        pump.pump()
        self.assertEqual([True], closed)

        # Let the last bit of network traffic flow.  This lets the server's
        # close acknowledgement through, at which point the client can close
        # the overall SSH connection.
        pump.pump()

        # Nothing useful can be done with the connection at this point, so the
        # endpoint should close it.
        self.assertTrue(client.transport.disconnecting)