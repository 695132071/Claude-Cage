package managedproxy

import (
	"errors"
	"path/filepath"
)

// Applied is the single source of truth for the private endpoint used by Cage.
func Applied(profile, endpoint, revision string) (Store, error) {
	p, err := Load(profile)
	if err != nil {
		return p, err
	}
	if _, ok := p.Find(p.Selected); !ok {
		return p, errors.New("尚未选择代理节点，请在网络与代理中添加来源并选择节点")
	}
	if _, err := p.Route(); err != nil {
		return p, err
	}
	if revision == "" || p.Revision == "" || revision != p.Revision || endpoint != p.Endpoint() {
		return p, errors.New("代理节点尚未应用或配置不一致，请重新应用节点；外网保持关闭")
	}
	// A previous core may still expose a retired account route even when the
	// selected environment route is valid. Revocation also covers that interval.
	if _, err := BoundInterface(filepath.Join(Dir(profile), "config.yaml")); errors.Is(err, ErrLocalProxyNode) {
		return p, err
	}
	return p, nil
}
