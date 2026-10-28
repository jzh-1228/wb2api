"""Persisted routing choices shared by the console and the request path."""
from admin.config import settings
from admin.models import SystemSetting

SELECTION_KEY = 'account_selection_mode'
INTERVAL_KEY = 'upstream_start_interval_ms'
SELECTION_MODES = (
    ('throughput', '吞吐优先', '优先选择并发占用比例最低的健康账号；会话粘性只在同负载账号间保留，兼顾到期积分。适合多人共享 Key。'),
    ('expiry_weighted', '到期排名加权', '按到期先后赋予 1/排名的权重；同到期时间等权，所有健康账号均有机会，繁忙账号适当降低权重。'),
    ('expiring', '严格到期优先', '先用最早到期的健康账号；同到期时间内优先空闲且最久未用的账号。'),
    ('lru', '均衡轮转', '优先在途最少、最久未用的健康账号，均匀分摊新会话。'),
    ('weighted', '综合加权', '综合剩余积分、临期积分和闲置时间，从权重最高的候选中分摊新会话。'),
    ('remain', '余额优先', '优先使用剩余积分最多的健康账号。'),
    ('oldest', '录入顺序优先', '优先使用最早录入的健康账号。'),
)
MODE_IDS = frozenset(item[0] for item in SELECTION_MODES)


def _value(db, key):
    # A request may wait while the administrator changes a setting. Refresh
    # existing ORM identities too; do not retain an earlier transaction's value.
    row = db.query(SystemSetting).populate_existing().filter(SystemSetting.key == key).first()
    return row.value if row is not None else None


def selection_mode(db):
    value = _value(db, SELECTION_KEY)
    if value in MODE_IDS:
        return value
    configured = getattr(settings, 'ACCOUNT_SELECT', 'expiry_weighted')
    return configured if configured in MODE_IDS else 'expiry_weighted'


def upstream_interval_ms(db):
    value = _value(db, INTERVAL_KEY)
    try:
        configured = getattr(settings, 'UPSTREAM_START_INTERVAL_MS', 75)
        return max(0, min(60000, int(value if value is not None else configured)))
    except (ValueError, TypeError):
        return 75


def save_value(db, key, value):
    row = db.get(SystemSetting, key)
    if row is None:
        row = SystemSetting(key=key)
        db.add(row)
    row.value = str(value)


def selection_policy(db):
    return {'mode': selection_mode(db), 'upstream_start_interval_ms': upstream_interval_ms(db), 'modes': [
        {'value': value, 'label': label, 'description': description}
        for value, label, description in SELECTION_MODES
    ]}
