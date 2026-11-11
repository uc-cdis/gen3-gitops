// Command loader validates rendered hatchery configs with hatchery's own code.
//
// It reads the manifest written by check_config.py (a JSON list of
// {"source": <values file>, "config": <rendered hatchery.json>}) and, for each
// entry, does two things:
//
//  1. A walk over the config alongside hatchery.HatcheryConfig's Go types
//     that reports every unknown key. Hatchery's real decode silently ignores
//     them, so a typo such as "cpu_limit" or "memmory-limit" otherwise
//     deploys fine and does nothing.
//  2. hatchery.LoadConfig, the function the service calls at startup, which
//     catches type errors (a string where a number goes) and hatchery's own
//     semantic checks (authz, license).
//
// Problems are printed as GitHub annotations on the offending line of the
// values file and written as JSON for report.py. The exit status is 0 even
// when configs have problems; non-zero means the loader itself failed.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"sort"
	"strings"

	"github.com/uc-cdis/hatchery/hatchery"
)

type entry struct {
	Source string `json:"source"`
	Config string `json:"config"`
}

// finding matches findings.Finding on the Python side.
type finding struct {
	Level     string `json:"level"`
	Check     string `json:"check"`
	File      string `json:"file"`
	Message   string `json:"message"`
	Hint      string `json:"hint"`
	Container string `json:"container"`
	Line      int    `json:"line"`
}

var (
	unknownFieldRe = regexp.MustCompile(`unknown field "([^"]+)"`)
	typeErrRe      = regexp.MustCompile(`Go struct field [\w.]*?\.?([\w-]+) of type (\S+)`)
)

// lineOf returns the 1-based line of the first occurrence of needle in the
// repo-relative file, or 0.
func lineOf(file, needle string) int {
	data, err := os.ReadFile(filepath.Join(repoRoot, file))
	if err != nil || needle == "" {
		return 0
	}
	for i, l := range strings.Split(string(data), "\n") {
		if strings.Contains(l, needle) {
			return i + 1
		}
	}
	return 0
}

var repoRoot = func() string {
	wd, _ := os.Getwd()
	// .github/hatchery/loader -> repo root
	return filepath.Clean(filepath.Join(wd, "..", "..", ".."))
}()

// explain turns a decode error into a finding with a line and a fix hint.
func explain(source string, err error) finding {
	msg := err.Error()
	f := finding{Level: "error", File: source, Check: "Hatchery loader"}
	if m := unknownFieldRe.FindStringSubmatch(msg); m != nil {
		f.Check = "Unknown key"
		f.Message = fmt.Sprintf("hatchery has no setting called %q, so it is silently ignored and does nothing.", m[1])
		f.Hint = "check the spelling (keys are case-sensitive, and most use dashes, e.g. \"memory-limit\") against hatchery's config.go, or remove it."
		f.Line = lineOf(source, `"`+m[1]+`"`)
	} else if m := typeErrRe.FindStringSubmatch(msg); m != nil {
		f.Check = "Wrong type"
		f.Message = fmt.Sprintf("%q has the wrong type (hatchery expects %s). Hatchery fails to load the whole config at startup, so no workspace on this commons can launch.", m[1], m[2])
		f.Hint = "numbers and booleans must be unquoted (\"target-port\": 8888), and strings quoted (\"cpu-limit\": \"2.0\")."
		f.Line = lineOf(source, `"`+m[1]+`"`)
	} else {
		f.Message = "hatchery.LoadConfig, which runs at service startup, rejected the config: " + msg
		f.Hint = "hatchery would crash-loop with this config; fix the setting named in the message."
	}
	if f.Line == 0 {
		f.Line = lineOf(source, "hatchery:")
	}
	return f
}

func annotate(f finding) {
	body := f.Message
	if f.Hint != "" {
		body += " — Fix: " + f.Hint
	}
	body = strings.NewReplacer("%", "%25", "\r", "", "\n", "%0A").Replace(body)
	title := "Hatchery loader: " + f.Check
	if f.Container != "" {
		title += " (" + f.Container + ")"
	}
	title = strings.NewReplacer("%", "%25", ",", "%2C", ":", "%3A", "\n", " ").Replace(title)
	fmt.Printf("::%s file=%s,line=%d,title=%s::%s\n", f.Level, f.File, f.Line, title, body)
}

var unmarshalerType = reflect.TypeOf((*json.Unmarshaler)(nil)).Elem()

// jsonFields maps the JSON keys of a struct type to their field types, the
// way encoding/json does (json tag name, else the Go field name).
func jsonFields(t reflect.Type) map[string]reflect.Type {
	out := map[string]reflect.Type{}
	for i := 0; i < t.NumField(); i++ {
		f := t.Field(i)
		if !f.IsExported() {
			continue
		}
		name := f.Name
		if tag, ok := f.Tag.Lookup("json"); ok {
			tagName := strings.Split(tag, ",")[0]
			if tagName == "-" {
				continue
			}
			if tagName != "" {
				name = tagName
			}
		}
		if f.Anonymous && f.Type.Kind() == reflect.Struct && name == f.Name {
			for k, v := range jsonFields(f.Type) {
				out[k] = v
			}
			continue
		}
		out[name] = f.Type
	}
	return out
}

// unknownKeys walks decoded JSON alongside hatchery's Go types and returns
// the path of every key hatchery would silently ignore. Unlike a strict
// json.Decoder it does not stop at the first problem.
func unknownKeys(v any, t reflect.Type, path string) []string {
	for t.Kind() == reflect.Pointer {
		t = t.Elem()
	}
	// Types with custom decoding (e.g. authz) define their own keys.
	if t.Implements(unmarshalerType) || reflect.PointerTo(t).Implements(unmarshalerType) {
		return nil
	}
	var out []string
	switch t.Kind() {
	case reflect.Struct:
		obj, ok := v.(map[string]any)
		if !ok {
			return nil // type mismatch: LoadConfig reports it
		}
		fields := jsonFields(t)
		keys := make([]string, 0, len(obj))
		for k := range obj {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			ft, ok := fields[k]
			if !ok {
				// encoding/json also matches keys case-insensitively.
				for name, typ := range fields {
					if strings.EqualFold(name, k) {
						ft, ok = typ, true
						break
					}
				}
			}
			if !ok {
				out = append(out, path+k)
				continue
			}
			out = append(out, unknownKeys(obj[k], ft, path+k+".")...)
		}
	case reflect.Slice, reflect.Array:
		if arr, ok := v.([]any); ok {
			for i, item := range arr {
				out = append(out, unknownKeys(item, t.Elem(), fmt.Sprintf("%s[%d].", strings.TrimSuffix(path, "."), i))...)
			}
		}
	case reflect.Map:
		if obj, ok := v.(map[string]any); ok {
			for k, item := range obj {
				out = append(out, unknownKeys(item, t.Elem(), path+k+".")...)
			}
		}
	}
	return out
}

// containerName returns the "name" of containers[i] for a key path, if any.
func containerName(cfg map[string]any, keyPath string) string {
	var i int
	if _, err := fmt.Sscanf(keyPath, "containers[%d]", &i); err != nil {
		return ""
	}
	if arr, ok := cfg["containers"].([]any); ok && i < len(arr) {
		if c, ok := arr[i].(map[string]any); ok {
			name, _ := c["name"].(string)
			return name
		}
	}
	return ""
}

// lineNear finds needle at or after the container's name line, else anywhere.
func lineNear(file, container, needle string) int {
	data, err := os.ReadFile(filepath.Join(repoRoot, file))
	if err != nil {
		return 0
	}
	lines := strings.Split(string(data), "\n")
	start := 0
	if container != "" {
		for i, l := range lines {
			if strings.Contains(l, `"name": "`+container+`"`) || strings.Contains(l, "name: "+container) {
				start = i
				break
			}
		}
	}
	for _, from := range []int{start, 0} {
		for i := from; i < len(lines); i++ {
			if strings.Contains(lines[i], needle) {
				return i + 1
			}
		}
	}
	return 0
}

func checkUnknownKeys(e entry) ([]finding, error) {
	data, err := os.ReadFile(e.Config)
	if err != nil {
		return nil, err
	}
	var cfg map[string]any
	if err := json.Unmarshal(data, &cfg); err != nil {
		return nil, err
	}
	var out []finding
	for _, p := range unknownKeys(cfg, reflect.TypeOf(hatchery.HatcheryConfig{}), "") {
		parts := strings.Split(p, ".")
		key := parts[len(parts)-1]
		container := containerName(cfg, p)
		out = append(out, finding{
			Level:     "error",
			Check:     "Unknown key",
			File:      e.Source,
			Container: container,
			Message:   fmt.Sprintf("hatchery has no setting %q (at %s), so it is silently ignored and does nothing.", key, p),
			Hint:      "check the spelling (keys are case-sensitive and most use dashes, e.g. \"memory-limit\") against hatchery's config.go, or remove it.",
			Line:      lineNear(e.Source, container, `"`+key+`"`),
		})
	}
	return out, nil
}

func main() {
	if len(os.Args) != 3 {
		fmt.Fprintln(os.Stderr, "usage: loader <manifest.json> <findings-out.json>")
		os.Exit(2)
	}
	raw, err := os.ReadFile(os.Args[1])
	if err != nil {
		log.Fatal(err)
	}
	var entries []entry
	if err := json.Unmarshal(raw, &entries); err != nil {
		log.Fatal(err)
	}

	findings := []finding{}
	for _, e := range entries {
		mine, err := checkUnknownKeys(e)
		if err != nil {
			mine = append(mine, explain(e.Source, err))
		}
		// LoadConfig logs the whole config on success; keep that out of the CI log.
		var logs bytes.Buffer
		if _, err := hatchery.LoadConfig(e.Config, log.New(&logs, "", 0)); err != nil {
			mine = append(mine, explain(e.Source, err))
		}
		for _, f := range mine {
			annotate(f)
		}
		if len(mine) == 0 {
			fmt.Printf("ok    %s\n", e.Source)
		} else {
			fmt.Printf("issue %s\n", e.Source)
		}
		findings = append(findings, mine...)
	}

	out, _ := json.MarshalIndent(findings, "", "  ")
	if err := os.MkdirAll(filepath.Dir(os.Args[2]), 0o755); err != nil {
		log.Fatal(err)
	}
	if err := os.WriteFile(os.Args[2], out, 0o644); err != nil {
		log.Fatal(err)
	}
	fmt.Printf("%d hatchery config(s) loaded, %d problem(s)\n", len(entries), len(findings))
}
