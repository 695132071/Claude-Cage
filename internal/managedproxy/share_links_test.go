package managedproxy

import (
	"reflect"
	"strings"
	"testing"
)

func TestV2rayNClipboardImportsConcreteNodes(t *testing.T) {
	const shares = "ss://YWVzLTEyOC1nY206YTpi@ss.example:8080#entry\r\n" +
		"socks://dTpwYXNz@exit.example:1080#final%20hop\n" +
		"vless://c988847e-62a0-4393-8d8f-f698626d0a90@vless.example:443?type=tcp&headerType=none&security=reality&encryption=none&sni=remote.example&fp=chrome&pbk=key&sid=abcd&flow=xtls-rprx-vision#reality\n" +
		"trojan://super%3Asecret@trojan.example:443?type=ws&path=%2Fgateway&host=ws.example&security=tls&sni=tls.example&alpn=h2%2Chttp%2F1.1&allowInsecure=0&insecure=0#ws"
	nodes, err := Parse([]byte(shares), "source")
	if err != nil || len(nodes) != 4 {
		t.Fatalf("imported %d, err=%v", len(nodes), err)
	}
	if cfg := nodes[0].Config; cfg["cipher"] != "aes-128-gcm" || cfg["password"] != "a:b" {
		t.Fatal("SS credentials changed")
	}
	if n := nodes[1]; n.Name != "final hop" || n.Type != "socks5" || n.Config["username"] != "u" || n.Config["password"] != "pass" {
		t.Fatal("SOCKS identity or remark changed")
	}
	if cfg := nodes[2].Config; cfg["tls"] != true || cfg["servername"] != "remote.example" || cfg["client-fingerprint"] != "chrome" || cfg["flow"] != "xtls-rprx-vision" ||
		!reflect.DeepEqual(cfg["reality-opts"], map[string]any{"public-key": "key", "short-id": "abcd"}) {
		t.Fatal("REALITY settings changed")
	}
	if cfg := nodes[3].Config; cfg["password"] != "super:secret" || cfg["sni"] != "tls.example" || cfg["skip-cert-verify"] != false ||
		!reflect.DeepEqual(cfg["ws-opts"], map[string]any{"path": "/gateway", "headers": map[string]any{"Host": "ws.example"}}) || !reflect.DeepEqual(cfg["alpn"], []string{"h2", "http/1.1"}) {
		t.Fatal("Trojan WebSocket or TLS settings changed")
	}
}

func TestNodeLinksRejectUnsupportedOptionsWithoutSecrets(t *testing.T) {
	for _, link := range []string{
		"vless://secret@example.test:443?type=xhttp",
		"trojan://secret@example.test:443?security=tls&pcs=pin",
		"trojan://secret@example.test:443?security=tls&allowInsecure=0&insecure=1",
		"ss://secret@ss.example:443",
		"ss://YWVzLTEyOC1nY206YQ@ss.example:443?plugin=secret",
		"socks://secret@remote.test:wrong",
		"v2rayN://secret",
		"vless://secret@example.test:443?type=ws&security=reality&fp=chrome&pbk=key",
		"vless://secret@example.test:443?type=ws&security=tls&alpn=h2",
		"trojan://secret@example.test:443?type=grpc&alpn=http%2F1.1",
		"trojan://secret@example.test:443?insecure=t",
	} {
		_, err := Parse([]byte("socks5://example.test:1080\n"+link), "source")
		if err == nil || strings.Contains(err.Error(), "secret") || !strings.Contains(err.Error(), "第 2 个节点") {
			t.Fatalf("expected redacted import error, got %v", err)
		}
	}
}

func TestNodeLinksUseTheSameHostListenerRuleAsYAML(t *testing.T) {
	var store Store
	if err := store.Import("copied nodes", "", []byte("socks://Og@127.0.0.1:10808#v2rayN\nsocks://Og@remote.example:1080#exit"), "source", 0, ""); err != nil {
		t.Fatal(err)
	}
	source := store.Sources[0]
	if len(source.Nodes) != 1 || source.Nodes[0].Name != "exit" || len(source.Skipped) != 1 || source.Skipped[0].Name != "v2rayN" {
		t.Fatal("clipboard import did not retain the remote node and flag the host listener")
	}
}

func TestShareLinksMatchTransportDefaults(t *testing.T) {
	plain, err := parseNodeLink("vless://user@proxy.example:443#literal+plus")
	if err != nil || plain["tls"] == true || plain["name"] != "literal+plus" {
		t.Fatalf("plain VLESS or URI fragment changed: %v", err)
	}
	ws, err := parseNodeLink("vless://user@proxy.example:80?type=ws")
	if err != nil || ws["ws-opts"].(map[string]any)["headers"].(map[string]any)["Host"] != "proxy.example" {
		t.Fatalf("plain WebSocket HTTP host changed: %v", err)
	}
	tls, err := parseNodeLink("trojan://password@proxy.example:443?type=ws&host=ws.example&path=%2Fone+two")
	if err != nil || tls["sni"] != "ws.example" || tls["ws-opts"].(map[string]any)["path"] != "/one+two" {
		t.Fatalf("Trojan TLS server name or WebSocket path changed: %v", err)
	}
	reality, err := parseNodeLink("vless://user@proxy.example:443?security=reality&pbk=key")
	if err != nil || reality["tls"] != true || reality["client-fingerprint"] != "chrome" {
		t.Fatalf("missing REALITY handshake default: %v", err)
	}
}
