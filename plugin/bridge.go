package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	v1 "github.com/Silo-Server/silo-plugin-sdk/pkg/pluginproto/silo/plugin/v1"
)

type object = map[string]any
type Config struct {
	SiloURL   string `json:"silo_url"`
	SiloKey   string `json:"silo_api_key"`
	BridgeKey string `json:"bridge_api_key"`
	Listen    string `json:"listen_address"`
	Name      string `json:"server_name"`
}

func defaults() Config {
	return Config{SiloURL: "http://127.0.0.1:8080", Listen: "0.0.0.0:8098", Name: "Silo (Tracearr bridge)"}
}
func (c Config) validate() error {
	u, e := url.Parse(c.SiloURL)
	if e != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return errors.New("set a valid native Silo HTTP(S) URL without credentials or query")
	}
	for _, k := range []string{c.SiloKey, c.BridgeKey} {
		if k == "" {
			return errors.New("set both Silo and bridge API keys")
		}
		for _, r := range k {
			if r <= 32 || r > 126 || r == '"' {
				return errors.New("API keys must be ASCII tokens without whitespace or quotes")
			}
		}
	}
	if c.SiloKey == c.BridgeKey {
		return errors.New("use a separate bridge API key")
	}
	if _, p, e := net.SplitHostPort(c.Listen); e != nil {
		return errors.New("listen address must be host:port")
	} else if n, e := strconv.Atoi(p); e != nil || n < 0 || n > 65535 {
		return errors.New("invalid listener port")
	}
	return nil
}

type apiError struct {
	code    int
	message string
}

func (e *apiError) Error() string     { return e.message }
func fail(code int, msg string) error { return &apiError{code, msg} }

type cachedRows struct {
	until time.Time
	rows  []object
}
type pendingReason struct {
	until time.Time
	text  string
}
type poster struct {
	url  string
	at   time.Time
	data []byte
	mime string
}
type Bridge struct {
	sessionMetadata map[string]cachedRows
	artworkRequests int
	artworkFailures int
	artworkError    string
	metadataGate    chan struct{}
	metadataNext    time.Time
	imageFlights    map[string]*imageLoad
	catalogFlight   *catalogLoad
	catalogRetryAt  time.Time
	catalogError    error
	catalogUpdated  time.Time
	cfg             Config
	http            *http.Client
	mu              sync.Mutex
	rows            map[string]cachedRows
	reasons         map[string]pendingReason
	posters         map[string]*poster
	controlUntil    time.Time
	controls        bool
	lastPoll        time.Time
	lastSuccess     time.Time
	pollError       string
	pollFailures    int
}

func newBridge(c Config) *Bridge {
	return &Bridge{metadataGate: make(chan struct{}, 1), imageFlights: map[string]*imageLoad{}, cfg: c, http: &http.Client{Timeout: 8 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, rows: map[string]cachedRows{}, reasons: map[string]pendingReason{}, posters: map[string]*poster{}}
}
func newHTTPServer(c Config) *http.Server {
	return &http.Server{Addr: c.Listen, Handler: newBridge(c), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 35 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 32 << 10}
}
func (b *Bridge) native(ctx context.Context, method, path string, q url.Values, body any, public bool, profile ...string) (object, error) {
	target := strings.TrimRight(b.cfg.SiloURL, "/") + "/api/v2" + path
	if len(q) > 0 {
		target += "?" + q.Encode()
	}
	var reader io.Reader
	if body != nil {
		data, e := json.Marshal(body)
		if e != nil {
			return nil, fail(502, "cannot encode Silo request")
		}
		reader = bytes.NewReader(data)
	}
	req, e := http.NewRequestWithContext(ctx, method, target, reader)
	if e != nil {
		return nil, fail(502, "invalid Silo URL")
	}
	if !public {
		req.Header.Set("Authorization", "Bearer "+b.cfg.SiloKey)
	}
	req.Header.Set("Accept", "application/json")
	if len(profile) > 0 {
		req.Header.Set("X-Profile-Id", profile[0])
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	metadataRead := method == "GET" && (path == "/admin/catalog/items" || path == "/profiles" || strings.HasPrefix(path, "/catalog/items/"))
	if metadataRead && b.metadataSlot(ctx) != nil {
		return nil, fail(503, "metadata lookup timed out; retry shortly")
	}
	resp, e := b.http.Do(req)
	if e != nil {
		return nil, fail(502, "Silo connection failed")
	}
	// Only retry read-only metadata. Stream termination is never replayed.
	for attempt := 0; metadataRead && resp.StatusCode == 429 && attempt < 3; attempt++ {
		delay := retryDelay(resp.Header.Get("Retry-After"), time.Now(), attempt)
		io.Copy(io.Discard, io.LimitReader(resp.Body, 8192))
		resp.Body.Close()
		if delay > 10*time.Minute || waitContext(ctx, delay) != nil {
			return nil, fail(503, "Silo metadata is rate limited; retry later")
		}
		if b.metadataSlot(ctx) != nil {
			return nil, fail(503, "metadata lookup timed out; retry shortly")
		}
		resp, e = b.http.Do(req.Clone(ctx))
		if e != nil {
			return nil, fail(502, "Silo connection failed")
		}
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		status := 502
		if resp.StatusCode == 429 {
			status = 503
		}
		if strings.HasPrefix(path, "/catalog/items/") && (resp.StatusCode == 404 || resp.StatusCode == 403) {
			status = 404
		}
		if method == "POST" && (resp.StatusCode == 403 || resp.StatusCode == 401) {
			status = 403
		}
		if method == "POST" && (resp.StatusCode == 404 || resp.StatusCode == 409) {
			status = resp.StatusCode
		}
		return nil, fail(status, fmt.Sprintf("Silo returned HTTP %d for %s", resp.StatusCode, path))
	}
	data, e := io.ReadAll(io.LimitReader(resp.Body, (8<<20)+1))
	if e != nil || len(data) > 8<<20 {
		return nil, fail(502, "invalid or oversized Silo response")
	}
	var result object
	if json.Unmarshal(data, &result) != nil || result == nil {
		return nil, fail(502, "invalid Silo JSON response")
	}
	return result, nil
}
func (b *Bridge) collection(ctx context.Context, path string, paginated bool) ([]object, error) {
	q := url.Values{}
	if paginated {
		q.Set("limit", "200")
	}
	rows := []object{}
	seen := map[string]bool{}
	for {
		result, e := b.native(ctx, "GET", path, q, nil, false)
		if e != nil {
			return nil, e
		}
		values, ok := result["items"].([]any)
		if !ok {
			return nil, fail(502, "invalid Silo collection")
		}
		for _, v := range values {
			row, ok := v.(map[string]any)
			if !ok {
				return nil, fail(502, "invalid Silo collection row")
			}
			rows = append(rows, row)
		}
		if len(rows) > 200000 {
			return nil, fail(502, "Silo collection exceeded 200,000 rows")
		}
		page := obj(result["page"])
		cursor := str(page["next_cursor"])
		if cursor == "" {
			if yes(page["has_more"]) {
				return nil, fail(502, "missing Silo cursor")
			}
			return rows, nil
		}
		if !paginated || seen[cursor] || len(values) == 0 {
			return nil, fail(502, "invalid Silo pagination")
		}
		seen[cursor] = true
		q.Set("cursor", cursor)
	}
}
func (b *Bridge) cached(ctx context.Context, path string, paginated bool) ([]object, error) {
	if path == "/admin/catalog/items" {
		return b.catalog(ctx)
	}
	b.mu.Lock()
	saved, ok := b.rows[path]
	b.mu.Unlock()
	if ok && time.Now().Before(saved.until) {
		return saved.rows, nil
	}
	rows, e := b.collection(ctx, path, paginated)
	if e != nil {
		return nil, e
	}
	b.mu.Lock()
	b.rows[path] = cachedRows{time.Now().Add(60 * time.Second), rows}
	b.mu.Unlock()
	return rows, nil
}
func (b *Bridge) canTerminate(ctx context.Context) bool {
	b.mu.Lock()
	if time.Now().Before(b.controlUntil) {
		v := b.controls
		b.mu.Unlock()
		return v
	}
	b.mu.Unlock()
	short, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	caps, e := b.native(short, "GET", "/admin/sessions/command-capabilities", nil, nil, false)
	supported := e == nil && yes(caps["terminate_revokes_authority"]) && contains(caps["actions"], "terminate")
	b.mu.Lock()
	b.controls = supported
	b.controlUntil = time.Now().Add(30 * time.Second)
	b.mu.Unlock()
	return supported
}
func (b *Bridge) verify(ctx context.Context) error {
	caps, e := b.native(ctx, "GET", "/admin/sessions/capabilities", nil, nil, false)
	if e != nil {
		return e
	}
	if !yes(caps["available"]) {
		return fail(502, "Silo session monitoring unavailable")
	}
	if _, e = b.native(ctx, "GET", "/admin/users", url.Values{"limit": {"1"}}, nil, false); e != nil {
		return e
	}
	if _, e = b.native(ctx, "GET", "/user/libraries", nil, nil, false); e != nil {
		return e
	}
	caps, e = b.native(ctx, "GET", "/admin/catalog/capabilities", nil, nil, false)
	if e != nil {
		return e
	}
	if !yes(caps["item_listing"]) {
		return fail(502, "Silo catalog monitoring unavailable")
	}
	return nil
}
func (p *pluginServer) TestConfiguration(ctx context.Context, req *v1.TestConfigurationRequest) (*v1.TestConfigurationResponse, error) {
	c, e := configEntries(req.GetConfig())
	if e == nil {
		e = c.validate()
	}
	if e == nil {
		e = newBridge(c).verify(ctx)
	}
	if e != nil {
		return &v1.TestConfigurationResponse{Ok: false, Message: e.Error()}, nil
	}
	return &v1.TestConfigurationResponse{Ok: true, Message: "Native Silo API is accessible. Open plugin status to start and test the listener."}, nil
}

var tokenPattern = regexp.MustCompile(`(?i)(?:^|[ ,])Token="([^"\r\n]+)"`)

func token(r *http.Request) string {
	for _, key := range []string{"Authorization", "X-Emby-Authorization"} {
		if m := tokenPattern.FindStringSubmatch(r.Header.Get(key)); len(m) > 1 {
			return m[1]
		}
	}
	if v := r.Header.Get("X-Emby-Token"); v != "" {
		return v
	}
	if v := r.URL.Query().Get("api_key"); v != "" {
		return v
	}
	return r.URL.Query().Get("apikey")
}
func respond(w http.ResponseWriter, code int, data any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(data)
}
func (b *Bridge) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	path := strings.ToLower(strings.TrimRight(r.URL.Path, "/"))
	if r.Method != "GET" || (path != "/health/live" && path != "/system/info/public") {
		if subtle.ConstantTimeCompare([]byte(token(r)), []byte(b.cfg.BridgeKey)) != 1 {
			respond(w, 401, object{"error": "invalid bridge API key"})
			return
		}
	}
	ctx, cancel := context.WithTimeout(r.Context(), 28*time.Second)
	defer cancel()
	var data any
	var e error
	status := 200
	if r.Method == "POST" {
		var body object
		if r.ContentLength > 16<<10 {
			e = fail(413, "request exceeds 16 KiB")
		} else {
			raw, readErr := io.ReadAll(http.MaxBytesReader(w, r.Body, 16<<10))
			if readErr != nil {
				e = fail(400, "cannot read request body")
			} else if len(raw) > 0 && (json.Unmarshal(raw, &body) != nil || body == nil) {
				e = fail(400, "expected a JSON object")
			} else {
				data, status, e = b.control(ctx, r.URL.Path, body)
			}
		}
	} else if r.Method == "GET" {
		if imageRoute.MatchString(r.URL.Path) {
			var image *poster
			image, e = b.image(ctx, r.URL.Path, r.URL.Query().Get("tag"))
			if e == nil {
				w.Header().Set("Content-Type", image.mime)
				w.Header().Set("Cache-Control", "no-store")
				w.Header().Set("X-Content-Type-Options", "nosniff")
				w.Write(image.data)
				return
			}
		} else {
			data, e = b.get(ctx, r)
		}
	} else {
		e = fail(405, "method unsupported")
	}
	if e != nil {
		status = 502
		var ae *apiError
		if errors.As(e, &ae) {
			status = ae.code
		}
		if status == 503 {
			w.Header().Set("Retry-After", "10")
		}
		respond(w, status, object{"error": e.Error()})
		return
	}
	respond(w, status, data)
}
func (b *Bridge) sessions(ctx context.Context) (result []object, err error) {
	defer func() {
		b.mu.Lock()
		defer b.mu.Unlock()
		b.lastPoll = time.Now().UTC()
		if err != nil {
			b.pollError = err.Error()
			b.pollFailures++
		} else {
			b.lastSuccess = b.lastPoll
			b.pollError = ""
			b.pollFailures = 0
		}
	}()
	ctx, cancel := context.WithTimeout(ctx, 9*time.Second)
	defer cancel()
	controls := b.canTerminate(ctx)
	rows, e := b.collection(ctx, "/admin/sessions", true)
	if e != nil {
		return nil, e
	}
	mapped := []object{}
	b.enrichSessionMetadata(ctx, rows)
	for _, row := range rows {
		s, e := mapSession(row)
		if e != nil {
			return nil, e
		}
		s["SupportsMediaControl"], s["SupportsRemoteControl"] = controls, controls
		item := obj(s["NowPlayingItem"])
		if tag := b.registerPoster(str(item["Id"]), str(row["poster_url"])); tag != "" {
			item["ImageTags"] = object{"Primary": tag}
		}
		mapped = append(mapped, s)
	}
	return mapped, nil
}
func (b *Bridge) get(ctx context.Context, r *http.Request) (any, error) {
	path := strings.ToLower(strings.TrimRight(r.URL.Path, "/"))
	switch path {
	case "/health/live":
		return object{"status": "ok", "version": version}, nil
	case "/health/ready":
		if e := b.verify(ctx); e != nil {
			return nil, e
		}
		return object{"status": "ready", "termination_supported": b.canTerminate(ctx)}, nil
	case "/system/info", "/system/info/public":
		id, e := b.native(ctx, "GET", "/system/identity", nil, nil, true)
		if e != nil {
			return nil, e
		}
		if str(id["server_id"]) == "" {
			return nil, fail(502, "missing Silo identity")
		}
		return object{"Id": id["server_id"], "ServerName": b.cfg.Name, "ProductName": "Silo Tracearr Bridge", "Version": version, "StartupWizardCompleted": true}, nil
	case "/users/me":
		return nil, fail(400, "API key has no user context")
	case "/auth/keys":
		if e := b.verify(ctx); e != nil {
			return nil, e
		}
		return object{"Items": []any{}, "TotalRecordCount": 0}, nil
	case "/sessions":
		return b.sessions(ctx)
	case "/users":
		rows, e := b.cached(ctx, "/admin/users", true)
		if e != nil {
			return nil, e
		}
		out := []object{}
		for _, row := range rows {
			out = append(out, mapUser(row))
		}
		return out, nil
	case "/library/virtualfolders":
		rows, e := b.cached(ctx, "/user/libraries", false)
		if e != nil {
			return nil, e
		}
		out := []object{}
		for _, row := range rows {
			if t := str(row["type"]); t == "movies" || t == "series" || t == "mixed" {
				out = append(out, mapLibrary(row))
			}
		}
		return out, nil
	case "/items":
		return b.items(ctx, r.URL.Query())
	case "/plugins":
		return []any{}, nil
	case "/quickconnect/enabled":
		return false, nil
	}
	if strings.HasPrefix(path, "/users/") && strings.Count(path, "/") == 2 {
		rows, e := b.cached(ctx, "/admin/users", true)
		if e != nil {
			return nil, e
		}
		for _, row := range rows {
			u := mapUser(row)
			if str(u["Id"]) == r.URL.Path[len("/Users/"):] {
				return u, nil
			}
		}
	}
	if path == "/system/activitylog/entries" || strings.HasSuffix(path, "/items") {
		return nil, fail(501, "historical imports are not implemented")
	}
	return nil, fail(404, "endpoint not implemented")
}
func (b *Bridge) items(ctx context.Context, input url.Values) (any, error) {
	q := url.Values{}
	for k, v := range input {
		q[strings.ToLower(k)] = v
	}
	allowed := map[string]bool{}
	for _, k := range strings.Split("ids,parentid,includeitemtypes,startindex,limit,sortby,sortorder,recursive,fields,ismissing,collapseboxsetitems,mindatelastsaved,enabletotalrecordcount,api_key,apikey", ",") {
		allowed[k] = true
	}
	for k := range q {
		if k == "filters" || k == "userid" {
			return nil, fail(501, "user history is not implemented")
		}
		if !allowed[k] {
			return nil, fail(400, "unsupported item query")
		}
	}
	start, limit := 0, 100
	var e error
	if q.Has("startindex") {
		start, e = strconv.Atoi(q.Get("startindex"))
		if e != nil {
			return nil, fail(400, "invalid start index")
		}
	}
	if q.Has("limit") {
		limit, e = strconv.Atoi(q.Get("limit"))
		if e != nil {
			return nil, fail(400, "invalid limit")
		}
	}
	if start < 0 || limit < 0 || limit > 10000 {
		return nil, fail(400, "invalid pagination")
	}
	parent := ""
	if q.Has("parentid") {
		parent, e = decodeID("library", q.Get("parentid"))
		if e != nil {
			return nil, e
		}
	}
	ids := map[string]bool{}
	if q.Has("ids") {
		for _, id := range strings.Split(q.Get("ids"), ",") {
			native, e := decodeID("item", id)
			if e != nil {
				return nil, e
			}
			ids[native] = true
		}
	}
	types := q.Get("includeitemtypes")
	if types == "" {
		types = "Movie,Series,Episode"
	}
	typeSet := map[string]bool{}
	for _, v := range strings.Split(strings.ToLower(types), ",") {
		typeSet[v] = true
	}
	var since time.Time
	if q.Get("mindatelastsaved") != "" {
		since, e = time.Parse(time.RFC3339Nano, q.Get("mindatelastsaved"))
		if e != nil {
			return nil, fail(400, "invalid date")
		}
	}
	rows, e := b.cached(ctx, "/admin/catalog/items", true)
	if e != nil {
		return nil, e
	}
	filtered := []object{}
	for _, row := range rows {
		if !typeSet[str(row["type"])] || (len(ids) > 0 && !ids[str(row["content_id"])]) || (parent != "" && !contains(row["library_ids"], parent)) {
			continue
		}
		if !since.IsZero() {
			at, e := time.Parse(time.RFC3339Nano, str(row["added_at"]))
			if e != nil {
				return nil, fail(502, "invalid catalog date")
			}
			if at.Before(since) {
				continue
			}
		}
		filtered = append(filtered, row)
	}
	sortBy := strings.ToLower(q.Get("sortby"))
	key := "content_id"
	switch sortBy {
	case "":
	case "datecreated":
		key = "added_at"
	case "sortname", "name":
		key = "title"
	default:
		return nil, fail(400, "unsupported sort field")
	}
	sort.SliceStable(filtered, func(i, j int) bool {
		a, c := strings.ToLower(str(filtered[i][key])), strings.ToLower(str(filtered[j][key]))
		if q.Get("sortorder") == "Descending" {
			return a > c
		}
		return a < c
	})
	out := []object{}
	for _, row := range filtered[min(start, len(filtered)):min(start+limit, len(filtered))] {
		out = append(out, mapItem(row))
	}
	return object{"Items": out, "TotalRecordCount": len(filtered), "StartIndex": start}, nil
}

var controlRoute = regexp.MustCompile(`(?i)^/Sessions/([A-Za-z0-9_-]{1,128})/(Message|Playing/Stop)$`)

func (b *Bridge) control(ctx context.Context, path string, body object) (any, int, error) {
	match := controlRoute.FindStringSubmatch(path)
	if match == nil {
		return nil, 0, fail(405, "only stream termination is supported")
	}
	if !b.canTerminate(ctx) {
		return nil, 0, fail(501, "Silo does not support durable termination")
	}
	id := match[1]
	if strings.EqualFold(match[2], "Message") {
		text, ok := body["Text"].(string)
		if !ok || len([]rune(text)) < 1 || len([]rune(text)) > 1024 {
			return nil, 0, fail(400, "reason must contain 1–1024 characters")
		}
		b.mu.Lock()
		defer b.mu.Unlock()
		for key, v := range b.reasons {
			if time.Now().After(v.until) {
				delete(b.reasons, key)
			}
		}
		if len(b.reasons) >= 256 {
			for key := range b.reasons {
				delete(b.reasons, key)
				break
			}
		}
		b.reasons[id] = pendingReason{time.Now().Add(time.Minute), text}
		return object{"queued": true}, 202, nil
	}
	if len(body) > 0 {
		return nil, 0, fail(400, "Stop does not accept a body")
	}
	b.mu.Lock()
	pending := b.reasons[id]
	b.mu.Unlock()
	reason := "Stream terminated by Tracearr"
	if time.Now().Before(pending.until) {
		reason = pending.text
	}
	receipt, e := b.native(ctx, "POST", "/admin/sessions/"+id+"/terminate", nil, object{"reason": reason}, false)
	if e != nil {
		return nil, 0, e
	}
	if str(receipt["session_id"]) != id || !yes(receipt["authority_revoked"]) || str(receipt["durable_state"]) != "stopped" {
		return nil, 0, fail(502, "Silo did not confirm session revocation")
	}
	b.mu.Lock()
	if b.reasons[id] == pending {
		delete(b.reasons, id)
	}
	b.mu.Unlock()
	return receipt, 200, nil
}

var imageRoute = regexp.MustCompile(`(?i)^/Items/([^/]+)/Images/Primary(?:/0)?$`)

func (b *Bridge) registerPoster(id, reference string) string {
	if reference == "" {
		return ""
	}
	base, e := url.Parse(strings.TrimRight(b.cfg.SiloURL, "/") + "/")
	if e != nil {
		return ""
	}
	ref, e := url.Parse(reference)
	if e != nil {
		return ""
	}
	u := base.ResolveReference(ref)
	prefix := strings.TrimRight(base.Path, "/") + "/api/v2/artwork/"
	if u.Scheme != base.Scheme || u.Host != base.Host || u.User != nil || u.Fragment != "" || !strings.HasPrefix(u.Path, prefix) || u.RawPath != "" {
		return ""
	}
	digest := sha256.Sum256([]byte(u.Path))
	tag := hex.EncodeToString(digest[:])[:24]
	key := id + ":" + tag
	b.mu.Lock()
	defer b.mu.Unlock()
	if existing := b.posters[key]; existing != nil {
		existing.url = u.String()
		existing.at = time.Now()
	} else {
		if len(b.posters) >= 16 {
			oldest := ""
			var at time.Time
			for k, p := range b.posters {
				if oldest == "" || p.at.Before(at) {
					oldest, at = k, p.at
				}
			}
			delete(b.posters, oldest)
		}
		b.posters[key] = &poster{url: u.String(), at: time.Now()}
	}
	return tag
}
func (b *Bridge) loadImage(ctx context.Context, path, tag string) (*poster, error) {
	id := imageRoute.FindStringSubmatch(path)[1]
	find := func() *poster {
		b.mu.Lock()
		defer b.mu.Unlock()
		var found *poster
		for k, p := range b.posters {
			if time.Since(p.at) > 5*time.Minute || (len(p.data) == 0 && time.Since(p.at) > 30*time.Second) {
				continue
			}
			if (tag != "" && k == id+":"+tag) || (tag == "" && strings.HasPrefix(k, id+":")) {
				if found == nil || p.at.After(found.at) {
					copy := *p
					found = &copy
				}
			}
		}
		return found
	}
	image := find()
	if image == nil {
		var libraryErr error
		if strings.HasPrefix(id, "item-") {
			libraryErr = b.libraryPoster(ctx, id)
			image = find()
		}
		if image == nil {
			_, sessionErr := b.sessions(ctx)
			image = find()
			if image == nil {
				if libraryErr != nil {
					return nil, libraryErr
				}
				if sessionErr != nil {
					return nil, sessionErr
				}
			}
		}
	}
	if image == nil {
		return nil, fail(404, "no artwork available for this item or image tag")
	}
	if len(image.data) > 0 {
		return image, nil
	}
	req, _ := http.NewRequestWithContext(ctx, "GET", image.url, nil)
	resp, e := b.http.Do(req)
	if e != nil {
		return nil, fail(502, "artwork connection failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fail(502, "artwork fetch failed")
	}
	mime := strings.Split(resp.Header.Get("Content-Type"), ";")[0]
	if mime != "image/jpeg" && mime != "image/png" && mime != "image/webp" && mime != "image/avif" && mime != "image/gif" {
		return nil, fail(502, "invalid artwork content type")
	}
	data, e := io.ReadAll(io.LimitReader(resp.Body, (8<<20)+1))
	if e != nil || len(data) == 0 || len(data) > 8<<20 {
		return nil, fail(502, "invalid artwork size")
	}
	image.data, image.mime = data, mime
	b.mu.Lock()
	for _, p := range b.posters {
		if p.url == image.url {
			p.data, p.mime = data, mime
		}
	}
	b.mu.Unlock()
	return image, nil
}
