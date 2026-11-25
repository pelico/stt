package web

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

const cookieName = "stt_session"

func secret(password string) []byte {
	h := sha256.Sum256([]byte("stt-session-v1:" + password))
	return h[:]
}

func sign(password string, issued int64) string {
	msg := strconv.FormatInt(issued, 10)
	mac := hmac.New(sha256.New, secret(password))
	mac.Write([]byte(msg))
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

func makeToken(password string) string {
	issued := time.Now().Unix()
	return strconv.FormatInt(issued, 10) + "." + sign(password, issued)
}

func verifyToken(token, password string) bool {
	if password == "" || token == "" {
		return false
	}
	parts := strings.SplitN(token, ".", 2)
	if len(parts) != 2 {
		return false
	}
	issued, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil {
		return false
	}
	if time.Now().Unix()-issued > 30*24*3600 || issued > time.Now().Unix()+60 {
		return false
	}
	want := sign(password, issued)
	return hmac.Equal([]byte(parts[1]), []byte(want))
}

func passwordMatches(supplied, password string) bool {
	if supplied == "" || password == "" {
		return false
	}
	return hmac.Equal([]byte(supplied), []byte(password))
}

// AuthGuard enforces the optional access token.
type AuthGuard struct {
	password    string
	mu          sync.Mutex
	fails       int
	lockedUntil time.Time
}

func NewAuthGuard(password string) *AuthGuard { return &AuthGuard{password: password} }

func (a *AuthGuard) Enabled() bool { return a.password != "" }

func (a *AuthGuard) CheckLogin(password string) (ok bool, locked bool, retryAfter int) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if time.Now().Before(a.lockedUntil) {
		return false, true, int(time.Until(a.lockedUntil).Seconds())
	}
	if !passwordMatches(password, a.password) {
		a.fails++
		if a.fails >= 8 {
			a.lockedUntil = time.Now().Add(5 * time.Minute)
			a.fails = 0
		}
		return false, false, 0
	}
	a.fails = 0
	return true, false, 0
}

func (a *AuthGuard) setSessionCookie(w http.ResponseWriter, r *http.Request) {
	secure := r.Header.Get("X-Forwarded-Proto") == "https" || r.TLS != nil
	http.SetCookie(w, &http.Cookie{
		Name: cookieName, Value: makeToken(a.password), Path: "/",
		MaxAge: 30 * 24 * 3600, HttpOnly: true, SameSite: http.SameSiteLaxMode, Secure: secure,
	})
}

func (a *AuthGuard) authorized(r *http.Request) bool {
	if !a.Enabled() {
		return true
	}
	supplied := r.Header.Get("X-Access-Token")
	if supplied == "" {
		supplied = r.URL.Query().Get("token")
	}
	if passwordMatches(supplied, a.password) {
		return true
	}
	cookie, err := r.Cookie(cookieName)
	if err != nil {
		return false
	}
	return verifyToken(cookie.Value, a.password)
}

// WSAuthorized checks WebSocket handshake (cookie or token query param).
func (a *AuthGuard) WSAuthorized(r *http.Request) bool {
	return a.authorized(r)
}

var _ = fmt.Sprintf
