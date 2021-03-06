package aln

import "fmt"

// Parser buffers a byte sequence and emits packets as they are read
// from a an input stream.

// Packet parsing state enumeration
const (
	STATE_FINDSTART = 0
	STATE_GET_CF    = 1
	STATE_GETHEADER = 2
	STATE_GETDATA   = 3
	STATE_GETCRC    = 4
)

// LinkState value enumerations (TODO support mesh routing)
const (
	NET_ROUTE   = 0x01 // packet contains route entry
	NET_SERVICE = 0x02 // packet contains service entry
	NET_QUERY   = 0x03 // packet is a request for content
)

type PacketCallback func(*Packet)
type OnCloseCallback func(Channel)

type Parser struct {
	frameBuffer []byte // clears on frame delimiter; contains no esc chars

	state        byte   // enumerated State
	delimCount   uint8  // counts '<' chars to detect new frames
	controlFlags uint16 // hamming-decoded first 2 bytes of frameBuffer

	headerIndex  uint8 // offset into header of next header byte
	headerLength uint8

	dataIndex  uint16 // offset into data of next data byte
	dataLength uint16 // local-hardware decoded datalength value

	crcIndex uint8 // offset into CRC of next CRC byte

	packetCallback PacketCallback
}

func NewParser(cb PacketCallback) *Parser {
	return &Parser{
		state:          STATE_FINDSTART,
		packetCallback: cb,
	}
}

// Clear resets the state of the parser following a frame
func (p *Parser) Clear() {
	p.frameBuffer = []byte{}
	p.state = STATE_FINDSTART
	p.delimCount = 0
	p.controlFlags = 0
	p.headerIndex = 0
	p.dataIndex = 0
	p.dataLength = 0
	p.crcIndex = 0
}

func (p *Parser) acceptPacket() {
	if pkt, err := ParsePacket(p.frameBuffer); err == nil {
		p.packetCallback(pkt)
	} else {
		fmt.Println("on acceptPacket, ParsePacket:" + err.Error())
	}
	p.Clear()
}

// IngestStream has intelligence to recognize the last
// byte of a packet without further input
func (p *Parser) IngestStream(buffer []byte) {
	for _, msg := range buffer {
		// check for escape char (occurs mid-frame)
		if msg == FRAME_ESCAPE {
			// fmt.Println("IngestStream FRAME_ESCAPE")
			if p.delimCount >= (FRAME_LEADER_LENGTH - 1) {
				p.delimCount = 0 // reset FRAME_LEADER detection
				continue         // drop the char from the stream
			}
		} else if msg == FRAME_LEADER {
			p.delimCount++
			if p.delimCount >= FRAME_LEADER_LENGTH {
				p.delimCount = 0
				p.headerIndex = 0
				p.frameBuffer = []byte{}
				p.state = STATE_GET_CF
				continue
			}
		} else { // not a framing byte; reset delim count
			p.delimCount = 0
		} // end if FRAME_ESCAPE

		// use current char in following state
		switch p.state {
		case STATE_FINDSTART:
			// Do Nothing, dump char

		case STATE_GET_CF:
			if p.headerIndex > MAX_HEADER_SIZE {
				p.state = STATE_FINDSTART
			} else {
				p.frameBuffer = append(p.frameBuffer, msg)
				p.headerIndex++
				if p.headerIndex == 2 {
					cf := bytesToINT16U(p.frameBuffer)
					p.controlFlags = CFHamDecode(cf)
					p.headerLength = HeaderLength(p.controlFlags)
					// fmt.Printf("Expected header length:%d\n", p.headerLength)
					p.state = STATE_GETHEADER
				}
			}

		case STATE_GETHEADER:
			if p.headerIndex >= MAX_HEADER_SIZE {
				p.state = STATE_FINDSTART
			} else {
				p.frameBuffer = append(p.frameBuffer, msg)
				p.headerIndex++
				if p.headerIndex >= p.headerLength {
					if p.controlFlags&CF_DATA != 0 {
						p.dataIndex = 0
						dataOffset := HeaderFieldOffset(p.controlFlags, CF_DATA)
						dataBytes := p.frameBuffer[dataOffset : dataOffset+2]
						p.dataLength = bytesToINT16U(dataBytes)
						p.state = STATE_GETDATA
					} else if p.controlFlags&CF_CRC != 0 {
						p.state = STATE_GETCRC
					} else {
						p.acceptPacket()
					}
				}
			}
		case STATE_GETDATA:
			p.frameBuffer = append(p.frameBuffer, msg)
			p.dataIndex++
			if p.dataIndex >= p.dataLength {
				if p.controlFlags&CF_CRC != 0 {
					p.state = STATE_GETCRC
				} else {
					p.acceptPacket()
				}
			}

		case STATE_GETCRC:
			p.frameBuffer = append(p.frameBuffer, msg)
			p.crcIndex++
			if p.crcIndex >= CRC_FIELD_SIZE {
				sz := len(p.frameBuffer)
				subframeBytes := p.frameBuffer[0 : sz-CRC_FIELD_SIZE]
				computedCRC := CRC32(subframeBytes)
				crcBytes := p.frameBuffer[sz-CRC_FIELD_SIZE : sz]
				expectedCRC := bytesToINT32U(crcBytes)
				if computedCRC != expectedCRC {
					// TODO error reporting
					p.Clear()
				} else {
					p.acceptPacket()
				}
			}
		}
	}
}
