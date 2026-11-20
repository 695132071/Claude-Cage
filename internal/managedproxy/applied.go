package managedproxy

import (
	"crypto/sha256"
	"errors"
)

// Applied is the single source of truth for the private endpoint used by Cage.
func Applied(profile, endpoint, revision string) (Store, error) {
	b, missing, err := readStore(profile)
	if err != nil {
		return Store{}, err
	}
	return appliedStore(b, missing, endpoint, revision)
}

func appliedStore(b []byte, missing bool, endpoint, revision string) (Store, error) {
	p, err := parseStore(b, missing)
	if err != nil {
		return p, err
	}
	if _, ok := p.Find(p.Selected); !ok {
		return p, errors.New("尚未选择代理节点，请在网络与代理中添加来源并选择节点")
	}
	if revision == "" || p.Revision == "" || revision != p.Revision || endpoint != p.Endpoint() {
		return p, errors.New("代理节点尚未应用或配置不一致，请重新应用节点；外网保持关闭")
	}
	if _, err := p.Route(); err != nil {
		return p, err
	}
	return p, nil
}

// AppliedCheck answers Applied's error for a caller that repeats the check
// every second. It still reads the store each time, so any change to its bytes
// (an in-place rewrite that keeps the modification time, a replacement or a
// removal) is decided on the same call; only identical bytes for the same
// endpoint and revision reuse the previous decision instead of reparsing.
// The decision depends on nothing else. Not safe for concurrent use.
type AppliedCheck struct {
	valid              bool
	sum                [sha256.Size]byte
	missing            bool
	endpoint, revision string
	err                error
}

func (c *AppliedCheck) Check(profile, endpoint, revision string) error {
	b, missing, err := readStore(profile)
	if err != nil {
		c.valid = false
		return err
	}
	sum := sha256.Sum256(b)
	if c.valid && c.sum == sum && c.missing == missing && c.endpoint == endpoint && c.revision == revision {
		return c.err
	}
	_, err = appliedStore(b, missing, endpoint, revision)
	if missing {
		// A missing store gets a fresh random credential; never reuse it.
		c.valid = false
		return err
	}
	*c = AppliedCheck{valid: true, sum: sum, endpoint: endpoint, revision: revision, err: err}
	return err
}
