package managedproxy

import (
	"bytes"
	"encoding/base64"
	"errors"
	"fmt"
	"net/url"
	"strconv"
	"strings"
	"unicode/utf8"
)

var errShareOptions = errors.New("分享链接含有暂不支持的协议或参数，请使用包含这些节点的 Clash/Mihomo YAML")

// v2rayN can copy several selected nodes as newline-separated share URIs.
// All converted fields still pass through parseNodes, just like pasted YAML.
func readNodeLinks(data []byte) ([]map[string]any, bool, error) {
	input := bytes.TrimSpace(data)
	first, _, _ := bytes.Cut(input, []byte("\n"))
	scheme, _, present := strings.Cut(string(first), "://")
	if !present || strings.ContainsAny(scheme, " \t\r\n:[]{}\"'") {
		return nil, false, nil
	}
	var nodes []map[string]any
	for line := range strings.Lines(string(input)) {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		if len(nodes) == 3000 {
			return nil, true, errors.New("每次最多导入 3000 个节点")
		}
		node, err := parseNodeLink(line)
		if err != nil {
			// URL parsing errors can contain the full credential-bearing URI.
			return nil, true, fmt.Errorf("第 %d 个节点：%w", len(nodes)+1, err)
		}
		nodes = append(nodes, node)
	}
	return nodes, true, nil
}

func parseNodeLink(line string) (map[string]any, error) {
	u, err := url.Parse(line)
	if err != nil || u.Hostname() == "" || (u.Path != "" && u.Path != "/") {
		return nil, errors.New("分享链接地址无效或格式暂不支持")
	}
	port, err := strconv.ParseUint(u.Port(), 10, 16)
	if err != nil || port == 0 {
		return nil, errors.New("分享链接缺少有效的节点端口")
	}
	// v2rayN escapes URI components, not HTML form values. Literal '+' is data.
	options, err := url.ParseQuery(strings.ReplaceAll(u.RawQuery, "+", "%2B"))
	if err != nil {
		return nil, errShareOptions
	}
	for _, values := range options {
		if len(values) != 1 {
			return nil, errors.New("分享链接的参数重复")
		}
	}

	name := u.Fragment
	if name == "" {
		name = u.Scheme + " " + u.Host
	}
	node := map[string]any{"type": u.Scheme, "name": name, "server": u.Hostname(), "port": int(port)}
	user := u.User.Username()
	password, hasPassword := u.User.Password()
	switch u.Scheme {
	case "socks", "socks5", "ss":
		// v2rayN's socks:// and SIP002 ss:// carry a base64 user:password.
		// SOCKS5 URLs can also use ordinary percent-encoded credentials.
		if !hasPassword && (u.Scheme == "ss" || (user != "" && u.Scheme == "socks")) {
			pair, ok := decodeCredentialPair(user)
			if !ok {
				return nil, errors.New("节点认证信息格式无效")
			}
			user, password, _ = strings.Cut(pair, ":")
		}
		if u.Scheme == "ss" {
			if user == "" || password == "" {
				return nil, errors.New("Shadowsocks 节点缺少加密方式或密码")
			}
			node["cipher"], node["password"] = user, password
		} else {
			node["type"], node["username"], node["password"] = "socks5", user, password
		}
	case "vless", "trojan":
		if user == "" || hasPassword {
			return nil, errors.New("分享链接的节点身份信息格式无效")
		}
		if u.Scheme == "vless" {
			node["uuid"] = user
			if encryption := takeOption(options, "encryption"); encryption != "" && encryption != "none" {
				return nil, errShareOptions
			}
			if flow := takeOption(options, "flow"); flow != "" {
				node["flow"] = flow
			}
		} else {
			node["password"] = user
		}
		if err := shareTransport(node, options, u.Scheme); err != nil {
			return nil, err
		}
	default:
		return nil, errShareOptions
	}
	if len(options) != 0 {
		return nil, errShareOptions
	}
	return node, nil
}

func decodeCredentialPair(s string) (string, bool) {
	for _, encoding := range []*base64.Encoding{base64.URLEncoding, base64.RawURLEncoding, base64.StdEncoding, base64.RawStdEncoding} {
		raw, err := encoding.DecodeString(s)
		if err == nil && utf8.Valid(raw) && bytes.ContainsRune(raw, ':') {
			return string(raw), true
		}
	}
	return "", false
}

// Consume each supported option once. Leftovers cause import to fail so an
// unhandled transport or certificate setting cannot silently change meaning.
func takeOption(values url.Values, key string) string {
	value := values.Get(key)
	values.Del(key)
	return value
}

func shareTransport(node map[string]any, options url.Values, protocol string) error {
	network := takeOption(options, "type")
	wsHost := ""
	switch network {
	case "", "tcp", "raw":
		if header := takeOption(options, "headerType"); header != "" && header != "none" {
			return errShareOptions
		}
	case "ws":
		ws := map[string]any{}
		if path := takeOption(options, "path"); path != "" {
			ws["path"] = path
		}
		wsHost = takeOption(options, "host")
		// Xray chooses Host, then the TLS server name, then the destination.
		// Set it explicitly because Mihomo can choose a synthetic HTTP host.
		header := wsHost
		if header == "" {
			header = options.Get("sni")
		}
		if header == "" {
			header = node["server"].(string)
		}
		ws["headers"] = map[string]any{"Host": header}
		node["network"], node["ws-opts"] = network, ws
	case "grpc":
		if mode := takeOption(options, "mode"); mode != "" && mode != "gun" {
			return errShareOptions
		}
		if takeOption(options, "authority") != "" {
			return errShareOptions
		}
		node["network"] = network
		node["grpc-opts"] = map[string]any{"grpc-service-name": takeOption(options, "serviceName")}
	default:
		return errShareOptions
	}

	security := takeOption(options, "security")
	if protocol == "trojan" && security == "" {
		security = "tls"
	}
	switch security {
	case "", "none":
		if protocol == "trojan" {
			return errShareOptions // bundled Trojan always negotiates TLS
		}
		return nil
	case "reality":
		// Mihomo's WebSocket code negotiates ordinary TLS on this path.
		if network == "ws" {
			return errShareOptions
		}
		key := takeOption(options, "pbk")
		if key == "" {
			return errors.New("REALITY 节点缺少公钥")
		}
		node["reality-opts"] = map[string]any{"public-key": key, "short-id": takeOption(options, "sid")}
	case "tls":
	default:
		return errShareOptions
	}
	sniKey := "sni"
	if protocol == "vless" {
		node["tls"] = true
		sniKey = "servername"
	}
	sni := takeOption(options, "sni")
	if sni == "" && security == "tls" && wsHost != "" {
		sni, _, _ = strings.Cut(wsHost, ",")
	}
	if sni != "" {
		node[sniKey] = sni
	}
	if fp := takeOption(options, "fp"); fp != "" {
		node["client-fingerprint"] = fp
	} else if security == "reality" {
		// Match the bundled core's default when importing a REALITY URI.
		node["client-fingerprint"] = "chrome"
	}
	if alpn := takeOption(options, "alpn"); alpn != "" {
		// These bundled transports set ALPN themselves regardless of node.alpn.
		if (network == "grpc" && alpn != "h2") || (network == "ws" && protocol == "vless" && alpn != "http/1.1") {
			return errShareOptions
		}
		node["alpn"] = strings.Split(alpn, ",")
	}
	// v2rayN emits both aliases for the same TLS setting.
	var skip *bool
	for _, key := range []string{"allowInsecure", "insecure"} {
		if value := takeOption(options, key); value != "" {
			enabled := value == "1"
			if (value != "0" && value != "1") || skip != nil && *skip != enabled {
				return errShareOptions
			}
			skip = &enabled
		}
	}
	if skip != nil {
		node["skip-cert-verify"] = *skip
	}
	return nil
}
