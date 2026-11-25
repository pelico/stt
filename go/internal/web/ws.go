package web

import (
	"encoding/json"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

// WSClient gives each WebSocket its own bounded send queue so a slow browser
// never blocks audio capture and VAD.
type WSClient struct {
	conn      *websocket.Conn
	queue     chan any
	listening bool
	mu        sync.RWMutex
	closeOnce sync.Once
}

func NewWSClient(conn *websocket.Conn) *WSClient {
	c := &WSClient{conn: conn, queue: make(chan any, 64)}
	go c.sendLoop()
	return c
}

func (c *WSClient) sendLoop() {
	for item := range c.queue {
		var err error
		switch v := item.(type) {
		case []byte:
			c.conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
			err = c.conn.WriteMessage(websocket.BinaryMessage, v)
		default:
			b, _ := json.Marshal(v)
			c.conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
			err = c.conn.WriteMessage(websocket.TextMessage, b)
		}
		if err != nil {
			c.Close()
			return
		}
	}
}

// Offer is non-blocking; drops oldest when full to keep latency low.
func (c *WSClient) Offer(item any) bool {
	select {
	case c.queue <- item:
		return true
	default:
		select {
		case <-c.queue:
		default:
		}
		select {
		case c.queue <- item:
			return true
		default:
			return false
		}
	}
}

func (c *WSClient) SetListening(on bool) {
	c.mu.Lock()
	c.listening = on
	c.mu.Unlock()
}
func (c *WSClient) IsListening() bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.listening
}

func (c *WSClient) Close() {
	c.closeOnce.Do(func() {
		close(c.queue)
		_ = c.conn.Close()
	})
}
