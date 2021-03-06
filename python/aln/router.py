from datetime import datetime
from threading import Lock, Thread
import random
from .packet import Packet, readINT16U, writeINT16U

AddressTypeSize = 2  # TODO make it easier to use longer addresses
bytesToAddressType = readINT16U

def makeNetQueryPacket():
    return Packet(netState=Packet.NET_QUERY)

def makeNetworkRouteSharePacket(srcAddr, destAddr, cost):
    data = writeINT16U(destAddr) + writeINT16U(cost)
    return Packet(netState=Packet.NET_ROUTE, srcAddr=srcAddr,data=data)

def parseNetworkRouteSharePacket(packet): # returns dest, next-hop, cost, err
    if packet.netState != Packet.NET_ROUTE:
        return (0, 0, 0, "parseNetworkRouteSharePacket: packet.NetState != NET_ROUTE")
    if len(packet.data) != 4:
        return (0, 0, 0, "parseNetworkRouteSharePacket: len(packet.Data) != 4")
    
    addr = readINT16U(packet.data[:2])
    cost = readINT16U(packet.data[2:])
    return addr, packet.srcAddr, cost, None

def makeNetworkServiceSharePacket(hostAddr, serviceID, serviceLoad):
    data = writeINT16U(hostAddr) + \
        writeINT16U(serviceID) + \
        writeINT16U(serviceLoad)
    return Packet(netState=Packet.NET_SERVICE, data=data)

# returns hostAddr, serviceID, load, error
def parseNetworkServiceSharePacket(packet):
    if packet.netState != Packet.NET_SERVICE:
        return (0, 0, 0, "parseNetworkRouteSharePacket: packet.NetState != NET_ROUTE")

    if len(packet.data) != AddressTypeSize+4:
        return (0, 0, 0, "parseNetworkRouteSharePacket: len(packet.data != 6")

    hostAddr = bytesToAddressType(packet.data[:AddressTypeSize])
    serviceID = readINT16U(packet.data[AddressTypeSize : AddressTypeSize+2])
    serviceLoad = readINT16U(packet.data[AddressTypeSize+2:])
    return hostAddr, serviceID, serviceLoad, None

class RemoteNode:
    def __init__(self, address, nextHop, cost, channel, lastSeen=datetime.now()):
        self.address = address
        self.nextHop = nextHop
        self.cost = cost
        self.channel = channel
        self.lastSeen = lastSeen

class Router(Thread):
    def __init__(self, selector, address=0):
        super(Router, self).__init__()
        self.lock = Lock()
        self.selector = selector # top-level application event loop
        self.address = address   # TODO dynamic address allocation
        self.channels = []       # pool of all channels for flood propagation
        self.contextMap = {}     # serviceHandlerMap
        self.remoteNodeMap = {}  # RemoteNodeMap // map[address]RemoteNodes
        self.serviceMap = {}     # map[address]callback registered local service handlers
        self.serviceLoadMap = {} # ServiceLoadMap // map[serviceID][address]load (remote services)
        self.serviceQueue = {}   # map[serviceID]PacketList
        self.stop = False
        self.daemon = True

    def run(self):
        while not self.stop:
            events = self.selector.select()
            for key, mask in events:
                callback = key.data
                callback(key.fileobj, mask)

    def handle_netstate(self, channel, packet):
        if packet.netState == Packet.NET_ROUTE: #  neighbor is sharing it's routing table')
            remoteAddress, nextHop, cost, err = parseNetworkRouteSharePacket(packet)
            if err is not None:
                print("err:",err)
            else:
                # msg = "NET_ROUTE to:[{rem}] via:[{next}] cost:{cost}"
                # print(msg.format(rem=remoteAddress, next=nextHop, cost=cost))
                if remoteAddress not in self.remoteNodeMap:
                    remoteNode = RemoteNode(remoteAddress, nextHop, cost, channel)
                    self.remoteNodeMap[remoteAddress] = remoteNode
                else:
                    remoteNode = self.remoteNodeMap[remoteAddress]
                    if remoteNode.channel not in self.channels or cost < remoteNode.cost or remoteNode.cost == 0:
                        remoteNode.nextHop = nextHop
                        remoteNode.channel = channel
                        remoteNode.cost = cost
                        #  relay update to other channels
                        p = makeNetworkRouteSharePacket(self.address, remoteAddress, cost+1)
                        for chan in self.channels:
                            if chan is not channel:
                                chan.send(p)

        if packet.netState == Packet.NET_SERVICE: # neighbor is sharing service load info
            address, serviceID, serviceLoad, err = parseNetworkServiceSharePacket(packet)
            if err is not None:
                print("error parsing NET_SERVICE: {0}", err)
            else:
                # print("NET_SERVICE node:{0}, service:{1}, load:{2}".format(address, serviceID, serviceLoad))
                if serviceID not in self.serviceLoadMap:
                    self.serviceLoadMap[serviceID] = {}
                self.serviceLoadMap[serviceID][address] = serviceLoad
                # first priority is to share the updates with neighbors
                for chan in self.channels:
                    if chan is not channel:
                        chan.send(packet)
                # second priority is to send queued packets waiting on service discovery
                if serviceID in self.serviceQueue:
                    for packet in self.serviceQueue[serviceID]:
                        if address in self.remoteNodeMap:
                            packet.destAddr = address
                            packet.nextAddr = self.remoteNodeMap[address].nextHop
                            # print("sending queued packet for serviceID {0} to host:{1} via:{2}".format(
                            #     serviceID, packet.destAddr, packet.nextAddr))
                            channel.send(packet)
                        else:
                            print("NET ERROR no route for advertised service: ", serviceID)
                    del(self.serviceQueue, serviceID)

        if packet.netState == Packet.NET_QUERY:     
            for routePacket in self.export_routes():
                channel.send(routePacket)
            for servicePacket in self.export_services():
                channel.send(servicePacket)

    def on_packet(self, channel, packet):
        if packet.controlFlags & Packet.CF_NETSTATE == Packet.CF_NETSTATE:
            with self.lock:
                self.handle_netstate(channel, packet)
        else:
            self.send(packet)

    def remove_channel(self, channel):
        with self.lock:
            self.channels.remove(channel)            

    def add_channel(self, channel):
        channel.on_close = self.remove_channel
        with self.lock:
            self.channels.append(channel)
            channel.listen(self.selector, self.on_packet)
            channel.send(makeNetQueryPacket())
    
    def send(self, packet):
        if packet.srcAddr == None:
            packet.srcAddr = self.address

        # print("send from {0} to {1} via {2}, serviceID:{3}, ctxID:{4}, datalen:{5}".format(
        #     packet.srcAddr, packet.destAddr, packet.nextAddr, packet.serviceID, packet.contextID, len(packet.data))
        # )

        packetHandler = None
        with self.lock:
            if packet.destAddr is None and packet.serviceID is not None:
                packet.destAddr = self.select_service(packet.serviceID)
                if not packet.destAddr:
                    if packet.serviceID in self.serviceQueue:
                        self.serviceQueue[packet.serviceID].append(packet)
                    else:
                        self.serviceQueue[packet.serviceID] = [packet]
                        return "service {0} unavailable, packet queued".format(packet.serviceID)
            
            if packet.destAddr is self.address:
                if packet.serviceID in self.serviceMap:
                    packetHandler = self.serviceMap[packet.serviceID]
                elif packet.contextID in self.contextMap:
                    packetHandler = self.contextMap[packet.contextID]
                else:
                    return ("send err, service:" + packet.serviceID + " context:"+ packet.contextID + " not registered")

            elif packet.nextAddr == self.address or packet.nextAddr == None:
                if packet.destAddr in self.remoteNodeMap:
                    route = self.remoteNodeMap[packet.destAddr]
                    packet.srcAddr = self.address
                    packet.nextAddr = route.nextHop
                    route.channel.send(packet)
                else:
                    return "no route for " + str(packet.destAddr)
            else:
                return "packet is unroutable; no action taken"
        if packetHandler:
            packetHandler(packet)
        return None

    def register_context_handler(self, callback):
        with self.lock:
            ctxID = random.randint(2, 65535)
            while ctxID in self.contextMap:
                ctxID = random.randint(2, 65535)
            self.contextMap[ctxID] = callback
            return ctxID

    def release_context(self, ctxID):
        with self.lock:
            self.contextMap.pop(ctxID, None)

    def register_service(self, serviceID, handler):
        with self.lock:
            self.serviceMap[serviceID] = handler

    def unregister_service(elf, serviceID):
        with self.lock:
            self.serviceMap.pol(serviceID, None)

    def select_service(self, serviceID):
        # return the address of service with lowest reported load or None
        if serviceID in self.serviceMap:
            return self.address

        minLoad = 0
        remoteAddress = None
        if serviceID in self.serviceLoadMap: 
            for addr in self.serviceLoadMap[serviceID]:
                if remoteAddress is None or self.serviceLoadMap[serviceID][addr] < minLoad:
                    minLoad = self.serviceLoadMap[serviceID][addr]
                    remoteAddress = addr
        return remoteAddress

    def export_routes(self):
        # compose routing table as an array of packets
        # one local route, with a cost of 1
        # for each remote route, our cost and add 1
        routes = [makeNetworkRouteSharePacket(self.address, self.address, 1)]
        for remoteAddress in self.remoteNodeMap:
            # TODO filter expired nodes by lastSeen 
            remoteNode = self.remoteNodeMap[remoteAddress]
            routes.append(makeNetworkRouteSharePacket(self.address, remoteAddress, remoteNode.cost+1))
        return routes

    def export_services(self):
        # compose a list of packets encoding the service table of this node
        services = []
        for serviceID in self.serviceMap:
            load = 0 # TODO measure load
            services.append(makeNetworkServiceSharePacket(self.address, serviceID, load))
        for serviceID in self.serviceLoadMap:
            loadMap = self.serviceLoadMap[serviceID]
            for remoteAddress in self.loadMap: # TODO sort by increasing load (first tx'd is lowest load)
                load = self.loadMap[remoteAddress]
                # TODO filter expired or unroutable entries
                services = append(services, makeNetworkServiceSharePacket(remoteAddress, serviceID, load))
        return services

    def close(self):
        self.stop = True
        for channel in self.channels:
            channel.close()
