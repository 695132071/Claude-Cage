package managedproxy

import (
	"encoding/base64"
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

func vmessLink(t *testing.T, fields map[string]any) string {
	t.Helper()
	b, err := json.Marshal(fields)
	if err != nil {
		t.Fatal(err)
	}
	return "vmess://" + base64.StdEncoding.EncodeToString(b)
}

func TestShareLinkProtocols(t *testing.T) {
	for _, tc := range []struct {
		name, link string
		want       map[string]any
	}{
		{"trojan", "trojan://pass%40word@trojan.example.com:443?sni=cdn.example.com&allowInsecure=1&type=ws&path=%2Fws&host=cdn.example.com#%F0%9F%87%AD%F0%9F%87%B0%20HK", map[string]any{
			"name": "🇭🇰 HK", "type": "trojan", "server": "trojan.example.com", "port": 443, "password": "pass@word", "sni": "cdn.example.com", "skip-cert-verify": true,
			"network": "ws", "ws-opts": map[string]any{"path": "/ws", "headers": map[string]any{"Host": "cdn.example.com"}}}},
		{"trojan-peer", "trojan://secret@198.51.100.7:8443?peer=sni.example.com#JP", map[string]any{
			"name": "JP", "type": "trojan", "server": "198.51.100.7", "port": 8443, "password": "secret", "sni": "sni.example.com"}},
		{"vless-reality", "vless://0b6c5b04-4f45-4f63-9d2e-6a7d5f3a2c11@reality.example.com:443?encryption=none&security=reality&sni=www.example.org&fp=chrome&pbk=PUBKEY&sid=6ba85179&flow=xtls-rprx-vision&type=tcp#US", map[string]any{
			"name": "US", "type": "vless", "server": "reality.example.com", "port": 443, "uuid": "0b6c5b04-4f45-4f63-9d2e-6a7d5f3a2c11", "tls": true, "servername": "www.example.org",
			"client-fingerprint": "chrome", "flow": "xtls-rprx-vision", "reality-opts": map[string]any{"public-key": "PUBKEY", "short-id": "6ba85179"}}},
		{"vless-grpc", "vless://uuid-1@grpc.example.com:443?security=tls&type=grpc&serviceName=svc&sni=grpc.example.com#SG", map[string]any{
			"name": "SG", "type": "vless", "server": "grpc.example.com", "port": 443, "uuid": "uuid-1", "tls": true, "servername": "grpc.example.com",
			"network": "grpc", "grpc-opts": map[string]any{"grpc-service-name": "svc"}}},
		{"vmess-ws-tls", vmessLink(t, map[string]any{"v": "2", "ps": "TW", "add": "vmess.example.com", "port": "443", "id": "uuid-2", "aid": "0", "scy": "auto", "net": "ws", "type": "none", "host": "cdn.example.com", "path": "/v", "tls": "tls", "sni": "cdn.example.com"}), map[string]any{
			"name": "TW", "type": "vmess", "server": "vmess.example.com", "port": 443, "uuid": "uuid-2", "alterId": 0, "cipher": "auto", "tls": true, "servername": "cdn.example.com",
			"network": "ws", "ws-opts": map[string]any{"path": "/v", "headers": map[string]any{"Host": "cdn.example.com"}}}},
		{"vmess-numeric-port", vmessLink(t, map[string]any{"ps": "KR", "add": "203.0.113.5", "port": 10086, "id": "uuid-3", "aid": 64, "net": "tcp"}), map[string]any{
			"name": "KR", "type": "vmess", "server": "203.0.113.5", "port": 10086, "uuid": "uuid-3", "alterId": 64, "cipher": "auto"}},
		{"ss-sip002", "ss://" + base64.RawURLEncoding.EncodeToString([]byte("aes-256-gcm:ss-pass")) + "@ss.example.com:8388#SS%201", map[string]any{
			"name": "SS 1", "type": "ss", "server": "ss.example.com", "port": 8388, "cipher": "aes-256-gcm", "password": "ss-pass"}},
		{"ss-2022-plain", "ss://2022-blake3-aes-128-gcm:YWJjZGVmZ2hpamtsbW5vcA%3D%3D@203.0.113.9:443#2022", map[string]any{
			"name": "2022", "type": "ss", "server": "203.0.113.9", "port": 443, "cipher": "2022-blake3-aes-128-gcm", "password": "YWJjZGVmZ2hpamtsbW5vcA=="}},
		{"ss-legacy", "ss://" + base64.StdEncoding.EncodeToString([]byte("chacha20-ietf-poly1305:legacy@legacy.example.com:8000")) + "#Legacy", map[string]any{
			"name": "Legacy", "type": "ss", "server": "legacy.example.com", "port": 8000, "cipher": "chacha20-ietf-poly1305", "password": "legacy"}},
		{"ss-obfs", "ss://" + base64.RawURLEncoding.EncodeToString([]byte("aes-128-gcm:p")) + "@obfs.example.com:80/?plugin=obfs-local%3Bobfs%3Dhttp%3Bobfs-host%3Dcdn.example.com#Obfs", map[string]any{
			"name": "Obfs", "type": "ss", "server": "obfs.example.com", "port": 80, "cipher": "aes-128-gcm", "password": "p", "plugin": "obfs", "plugin-opts": map[string]any{"mode": "http", "host": "cdn.example.com"}}},
		{"hysteria2", "hysteria2://auth-pass@hy.example.com:443/?sni=hy.example.com&insecure=1&obfs=salamander&obfs-password=ob&mport=20000-30000#HY2", map[string]any{
			"name": "HY2", "type": "hysteria2", "server": "hy.example.com", "port": 443, "password": "auth-pass", "sni": "hy.example.com", "skip-cert-verify": true,
			"obfs": "salamander", "obfs-password": "ob", "mport": "20000-30000"}},
		{"hy2-alias", "hy2://pw@198.51.100.20:8443#Alias", map[string]any{
			"name": "Alias", "type": "hysteria2", "server": "198.51.100.20", "port": 8443, "password": "pw"}},
		{"tuic", "tuic://uuid-4:tuic-pass@tuic.example.com:443?congestion_control=bbr&udp_relay_mode=native&alpn=h3&sni=tuic.example.com&allow_insecure=0#TUIC", map[string]any{
			"name": "TUIC", "type": "tuic", "server": "tuic.example.com", "port": 443, "uuid": "uuid-4", "password": "tuic-pass", "congestion-controller": "bbr",
			"udp-relay-mode": "native", "alpn": []string{"h3"}, "sni": "tuic.example.com"}},
		{"anytls", "anytls://any-pass@any.example.com:443?sni=any.example.com&fp=chrome#AnyTLS", map[string]any{
			"name": "AnyTLS", "type": "anytls", "server": "any.example.com", "port": 443, "password": "any-pass", "sni": "any.example.com", "client-fingerprint": "chrome"}},
		{"unnamed", "trojan://pw@203.0.113.1:443", map[string]any{
			"name": "trojan 203.0.113.1:443", "type": "trojan", "server": "203.0.113.1", "port": 443, "password": "pw"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := parseShareLink(tc.link)
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("got  %#v\nwant %#v", got, tc.want)
			}
		})
	}
}

func TestShareLinkRejectsAmbiguousEntries(t *testing.T) {
	for _, tc := range []struct{ name, link, reason string }{
		{"ssr", "ssr://c2VjcmV0", "协议暂不支持"},
		{"socks", "socks://dXNlcjpwYXNz@203.0.113.1:1080#S", "协议暂不支持"},
		{"vless-xhttp", "vless://u@h.example.com:443?security=tls&type=xhttp#X", "传输方式暂不支持"},
		{"vless-kcp", "vless://u@h.example.com:443?type=kcp#K", "传输方式暂不支持"},
		{"vless-encryption", "vless://u@h.example.com:443?encryption=mlkem768x25519plus#E", "encryption"},
		{"vless-reality-no-key", "vless://u@h.example.com:443?security=reality#R", "公钥"},
		{"trojan-http-header", "trojan://p@h.example.com:443?type=tcp&headerType=http#H", "伪装头"},
		{"vmess-not-json", "vmess://not-base64!!", "v2rayN"},
		{"vmess-quic", vmessLink(t, map[string]any{"add": "h.example.com", "port": 443, "id": "u", "net": "quic"}), "传输方式暂不支持"},
		{"ss-v2ray-plugin", "ss://YWVzLTEyOC1nY206cA@h.example.com:443/?plugin=v2ray-plugin%3Bmode%3Dwebsocket#V", "插件暂不支持"},
		{"tuic-v4-token", "tuic://token@h.example.com:443#T", "uuid:password"},
		{"no-port", "trojan://p@h.example.com#N", "格式无效"},
		{"bad-port", "anytls://p@h.example.com:70000#N", "格式无效"},
		{"no-credential", "anytls://h.example.com:443#N", "格式无效"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, err := parseShareLink(tc.link)
			if err == nil || !strings.Contains(err.Error(), tc.reason) {
				t.Fatalf("error = %v, want %q", err, tc.reason)
			}
		})
	}
}

func TestShareLinkSubscriptionImportsPlainAndBase64(t *testing.T) {
	list := strings.Join([]string{
		"trojan://secret-one@203.0.113.1:443?sni=a.example.com#A",
		"",
		"ssr://c2VjcmV0LXNzcg",
		"vless://uuid-x@203.0.113.2:443?type=xhttp#Unsupported-transport",
		"hysteria2://secret-two@203.0.113.3:443/?mport=bad-range#Bad-ports",
		"anytls://secret-three@203.0.113.4:443#B",
	}, "\r\n")
	for name, data := range map[string]string{
		"plain":      list,
		"base64":     base64.StdEncoding.EncodeToString([]byte(list)),
		"base64-raw": base64.RawURLEncoding.EncodeToString([]byte(list)),
		"wrapped":    wrap(base64.StdEncoding.EncodeToString([]byte(list)), 76),
	} {
		t.Run(name, func(t *testing.T) {
			nodes, skipped, err := parseNodes([]byte(data), "links")
			if err != nil {
				t.Fatal(err)
			}
			if len(nodes) != 2 || nodes[0].Name != "A" || nodes[1].Name != "B" || nodes[0].Config["password"] != "secret-one" {
				t.Fatalf("nodes = %+v", nodes)
			}
			if len(skipped) != 3 {
				t.Fatalf("every unusable entry must be reported: %+v", skipped)
			}
			report, _ := json.Marshal(skipped)
			for _, secret := range []string{"secret-ssr", "c2VjcmV0", "uuid-x", "secret-two", "203.0.113"} {
				if strings.Contains(string(report), secret) {
					t.Fatalf("skip report leaked link data %q: %s", secret, report)
				}
			}
			if !strings.Contains(string(report), "Unsupported-transport") || !strings.Contains(string(report), "协议暂不支持") {
				t.Fatalf("skip report lost names or reasons: %s", report)
			}
		})
	}
}

func TestShareLinkSingleNodePaste(t *testing.T) {
	nodes, _, err := parseNodes([]byte("  vless://uuid-y@203.0.113.8:443?security=tls&sni=v.example.com#Pasted \n"), "paste")
	if err != nil || len(nodes) != 1 || nodes[0].Type != "vless" || nodes[0].Config["servername"] != "v.example.com" {
		t.Fatalf("pasted link = %+v, %v", nodes, err)
	}
}

func TestNonLinkContentKeepsYAMLError(t *testing.T) {
	for _, data := range []string{"not: [valid", "aGVsbG8gd29ybGQ=", "<html>login</html>", "{\"outbounds\": []}"} {
		if _, _, err := parseNodes([]byte(data), "x"); err == nil {
			t.Fatalf("%q imported", data)
		}
	}
}

func wrap(s string, n int) string {
	var b strings.Builder
	for len(s) > n {
		b.WriteString(s[:n] + "\n")
		s = s[n:]
	}
	b.WriteString(s)
	return b.String()
}
