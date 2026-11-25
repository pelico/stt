package store

import (
	"database/sql"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	_ "modernc.org/sqlite"
)

// Transcript is one recognized sentence.
type Transcript struct {
	ID          int64  `json:"id"`
	Text        string `json:"text"`
	DurationSec int    `json:"duration_sec"`
	Engine      string `json:"engine"`
	Timestamp   string `json:"timestamp"`
}

// HistoryDB stores transcripts (asr.db).
type HistoryDB struct {
	db *sql.DB
}

func OpenHistory(path string) (*HistoryDB, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	schema := `CREATE TABLE IF NOT EXISTS transcripts (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		text TEXT NOT NULL,
		duration_sec INTEGER NOT NULL DEFAULT 0,
		engine TEXT NOT NULL DEFAULT '',
		timestamp TEXT NOT NULL
	);
	CREATE INDEX IF NOT EXISTS idx_transcripts_timestamp ON transcripts(timestamp DESC);`
	if _, err = db.Exec(schema); err != nil {
		db.Close()
		return nil, err
	}
	return &HistoryDB{db: db}, nil
}

func (h *HistoryDB) Close() error { return h.db.Close() }

func (h *HistoryDB) Save(text string, durationSec int, engine string, capturedAt time.Time) (*Transcript, error) {
	ts := capturedAt.UTC().Format("2006-01-02T15:04:05")
	res, err := h.db.Exec(
		"INSERT INTO transcripts (text, duration_sec, engine, timestamp) VALUES (?, ?, ?, ?)",
		text, durationSec, engine, ts,
	)
	if err != nil {
		return nil, err
	}
	id, _ := res.LastInsertId()
	return &Transcript{ID: id, Text: text, DurationSec: durationSec, Engine: engine, Timestamp: ts}, nil
}

func (h *HistoryDB) List(limit, offset int, keyword string) ([]Transcript, int, error) {
	if limit < 1 || limit > 500 {
		limit = 100
	}
	if offset < 0 {
		offset = 0
	}
	where, args := "", []any{}
	if keyword != "" {
		// Escape LIKE wildcards so user input is matched literally.
		esc := strings.NewReplacer(`\`, `\\`, `%`, `\%`, `_`, `\_`).Replace(keyword)
		where = " WHERE text LIKE ? ESCAPE '\\'"
		args = append(args, "%"+esc+"%")
	}
	var total int
	if err := h.db.QueryRow("SELECT COUNT(*) FROM transcripts"+where, args...).Scan(&total); err != nil {
		return nil, 0, err
	}
	q := "SELECT id, text, duration_sec, engine, timestamp FROM transcripts" + where +
		" ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?"
	rows, err := h.db.Query(q, append(args, limit, offset)...)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()
	items := make([]Transcript, 0, limit)
	for rows.Next() {
		var t Transcript
		if err := rows.Scan(&t.ID, &t.Text, &t.DurationSec, &t.Engine, &t.Timestamp); err != nil {
			return nil, 0, err
		}
		items = append(items, t)
	}
	return items, total, rows.Err()
}

// Clear deletes all recognition history rows.
func (h *HistoryDB) Clear() error {
	_, err := h.db.Exec("DELETE FROM transcripts")
	return err
}

// PendingItem is one audio segment waiting for recognition.
type PendingItem struct {
	ID        int64
	WallISO   string
	PCM       []byte
	Attempts  int
	CreatedAt float64
	Size      int
}

// PendingStore is a durable FIFO queue backed by SQLite WAL (pending.db).
type PendingStore struct {
	mu   sync.Mutex
	db   *sql.DB
	path string
}

func OpenPending(path string) (*PendingStore, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	schema := `PRAGMA journal_mode=WAL;
	PRAGMA synchronous=NORMAL;
	CREATE TABLE IF NOT EXISTS pending (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		wall_iso TEXT NOT NULL,
		created_at REAL NOT NULL,
		pcm BLOB NOT NULL,
		attempts INTEGER NOT NULL DEFAULT 0,
		size INTEGER NOT NULL
	);
	CREATE INDEX IF NOT EXISTS idx_pending_id ON pending(id);`
	if _, err = db.Exec(schema); err != nil {
		db.Close()
		return nil, err
	}
	return &PendingStore{db: db, path: path}, nil
}

func (p *PendingStore) Close() error { return p.db.Close() }

// Put inserts a segment and trims by item count / total size. Returns (count, dropped).
func (p *PendingStore) Put(pcm []byte, wallISO string, maxItems, maxMB int) (int, int, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	tx, err := p.db.Begin()
	if err != nil {
		return 0, 0, err
	}
	defer tx.Rollback()
	now := float64(time.Now().UnixNano()) / 1e9
	if _, err = tx.Exec(
		"INSERT INTO pending (wall_iso, created_at, pcm, attempts, size) VALUES (?, ?, ?, 0, ?)",
		wallISO, now, pcm, len(pcm),
	); err != nil {
		return 0, 0, err
	}
	dropped := 0
	if maxItems > 0 {
		res, err := tx.Exec(
			"DELETE FROM pending WHERE id IN (SELECT id FROM pending ORDER BY id DESC LIMIT -1 OFFSET ?)",
			maxItems,
		)
		if err != nil {
			return 0, 0, err
		}
		n, _ := res.RowsAffected()
		dropped += int(n)
	}
	if maxMB > 0 {
		rows, err := tx.Query("SELECT id, size FROM pending ORDER BY id")
		if err != nil {
			return 0, 0, err
		}
		type row struct {
			id   int64
			size int
		}
		var all []row
		for rows.Next() {
			var r row
			if err := rows.Scan(&r.id, &r.size); err != nil {
				rows.Close()
				return 0, 0, err
			}
			all = append(all, r)
		}
		rows.Close()
		total := 0
		for _, r := range all {
			total += r.size
		}
		limit := maxMB * 1024 * 1024
		doomed := []int64{}
		for _, r := range all {
			if total <= limit {
				break
			}
			doomed = append(doomed, r.id)
			total -= r.size
		}
		for _, id := range doomed {
			if _, err := tx.Exec("DELETE FROM pending WHERE id = ?", id); err != nil {
				return 0, 0, err
			}
			dropped++
		}
	}
	var count int
	if err := tx.QueryRow("SELECT COUNT(*) FROM pending").Scan(&count); err != nil {
		return 0, 0, err
	}
	if err := tx.Commit(); err != nil {
		return 0, 0, err
	}
	return count, dropped, nil
}

// PeekBatch returns the oldest items up to maxItems / maxBytes.
func (p *PendingStore) PeekBatch(maxItems int, maxBytes int) ([]PendingItem, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	rows, err := p.db.Query(
		"SELECT id, wall_iso, created_at, pcm, attempts, size FROM pending ORDER BY id LIMIT ?",
		max(1, maxItems),
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []PendingItem{}
	total := 0
	for rows.Next() {
		var it PendingItem
		if err := rows.Scan(&it.ID, &it.WallISO, &it.CreatedAt, &it.PCM, &it.Attempts, &it.Size); err != nil {
			return nil, err
		}
		if len(out) > 0 && total+it.Size > maxBytes {
			break
		}
		out = append(out, it)
		total += it.Size
	}
	return out, rows.Err()
}

func (p *PendingStore) DeleteMany(ids []int64) error {
	if len(ids) == 0 {
		return nil
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	ph := strings.TrimSuffix(strings.Repeat("?,", len(ids)), ",")
	args := make([]any, len(ids))
	for i, id := range ids {
		args[i] = id
	}
	_, err := p.db.Exec("DELETE FROM pending WHERE id IN ("+ph+")", args...)
	return err
}

// BumpMany increments attempts; deletes items reaching maxAttempts. Returns removed count.
func (p *PendingStore) BumpMany(ids []int64, maxAttempts int) (int, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	tx, err := p.db.Begin()
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	removed := 0
	for _, id := range ids {
		var attempts int
		if err := tx.QueryRow("SELECT attempts FROM pending WHERE id = ?", id).Scan(&attempts); err != nil {
			continue
		}
		attempts++
		if maxAttempts > 0 && attempts >= maxAttempts {
			if _, err := tx.Exec("DELETE FROM pending WHERE id = ?", id); err == nil {
				removed++
			}
		} else {
			_, _ = tx.Exec("UPDATE pending SET attempts = ? WHERE id = ?", attempts, id)
		}
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return removed, nil
}

// Stats returns (count, totalBytes, lagSeconds).
func (p *PendingStore) Stats() (int, int64, float64) {
	p.mu.Lock()
	defer p.mu.Unlock()
	var count int
	var total int64
	var oldest float64
	if err := p.db.QueryRow(
		"SELECT COUNT(*), COALESCE(SUM(size),0), COALESCE(MIN(created_at),0) FROM pending",
	).Scan(&count, &total, &oldest); err != nil {
		return 0, 0, 0
	}
	lag := 0.0
	if count > 0 && oldest > 0 {
		lag = float64(time.Now().UnixNano())/1e9 - oldest
		if lag < 0 {
			lag = 0
		}
	}
	return count, total, lag
}

func (p *PendingStore) Count() int {
	n, _, _ := p.Stats()
	return n
}

func (p *PendingStore) MaybeVacuum(thresholdMB int) bool {
	if p.Count() != 0 {
		return false
	}
	st, err := os.Stat(p.path)
	if err != nil || st.Size() < int64(thresholdMB)*1024*1024 {
		return false
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	_, err = p.db.Exec("VACUUM")
	return err == nil
}

// RTSPHistory stores recent RTSP URLs in a JSON file.
type RTSPHistory struct {
	mu   sync.Mutex
	path string
}

func NewRTSPHistory(path string) *RTSPHistory {
	return &RTSPHistory{path: path}
}

func (r *RTSPHistory) load() []string {
	b, err := os.ReadFile(r.path)
	if err != nil {
		return []string{}
	}
	var items []string
	if err := json.Unmarshal(b, &items); err != nil {
		return []string{}
	}
	return items
}

func (r *RTSPHistory) save(items []string) {
	_ = os.MkdirAll(filepath.Dir(r.path), 0o755)
	b, _ := json.Marshal(items)
	_ = os.WriteFile(r.path, b, 0o644)
}

func (r *RTSPHistory) List() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.load()
}

func (r *RTSPHistory) Add(url string) []string {
	if strings.TrimSpace(url) == "" {
		return r.List()
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	items := r.load()
	out := []string{url}
	for _, u := range items {
		if u != url && len(out) < 10 {
			out = append(out, u)
		}
	}
	r.save(out)
	return out
}

func (r *RTSPHistory) Remove(url string) []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	items := r.load()
	out := make([]string, 0, len(items))
	for _, u := range items {
		if u != url {
			out = append(out, u)
		}
	}
	r.save(out)
	return out
}
