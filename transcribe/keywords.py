"""錄音轉譯用的詞彙表（面試、會議、通話）。

`initial_prompt` 是 Whisper 的**風格與拼寫提示**，不是字典。它會被送進 decoder
當作前文，讓模型傾向照著這些寫法輸出專有名詞。

🔴 硬限制：initial_prompt 最多 224 個 token（Whisper context 448 的一半），
超過的部分會被**靜默截斷**——不會報錯，只是後面的詞完全沒作用。
所以不要把所有詞塞進去。實測下來連挑 2~3 組都會互相干擾，**預設只用 `core`**（見下方實測）。

而且提示是有副作用的，這不是推測，是在本機實測出來的：

| 詞組（token） | Ansible | Grafana | 標點 |
|---|---|---|---|
| 不加 prompt      | Ancibo ✗   | Graphana ✗ | 簡體中文 🔴 |
| core (72)        | Ansible ✓  | Graphana ✗ | 正常 |
| core,mine (101)  | （被 mine 裡的縮寫帶偏）✗ | Graphana ✗ | 正常 |
| core,observability (149) | Ncibo ✗ | Grafana ✓ | 全形冒號＋亂碼 ✗ |

三個結論：
1. **不加 prompt 會輸出簡體中文。** 對台灣求職逐字稿這是最嚴重的一個問題，
   所以 prompt 一定要用繁體寫（本檔全繁體）。
2. **prompt 越長，個別詞的效果越弱。** core 只有 72 token 時 Ansible 是對的；
   加上 mine（多了一個學校縮寫）Ansible 就被硬湊成那個縮寫的變體，加上 observability 就變 Ncibo。
3. **超過約 120 token 開始出現標點風格污染**（全形冒號取代逗號、偶發亂碼）。

所以實務建議：**預設只用 `core`，這場錄音真正需要的 3~5 個詞用 `--extra` 補**。
不要為了「保險」多選詞組——多選會讓你原本會對的詞變錯。

開頭句也會影響繁簡，改之前要實測（同一份面試錄音、core 詞組、簡體字不計「台」）：

| 開頭句 | 簡體字 | 與原句相似度 |
|---|---|---|
| 以下是一段技術面試的對話（原句） | 6 | 同一句重跑 0.998 |
| 以下是一段對話錄音 | 58 | 0.923 |
| 以下是一段繁體中文的對話 | 28 | 0.937 |
| 以下是一段技術對話（現行） | 6 | 0.998 |

拿掉「技術」就會變差，明寫「繁體中文」也補不回來；「面試」兩字沒有作用。
"""

GROUPS: dict[str, list[str]] = {
    # --- 核心：幾乎每場技術面試都會出現，預設就帶這組 ---
    "core": [
        "Linux", "Proxmox VE", "Kubernetes", "Docker", "Ansible",
        "Terraform", "CI/CD", "GitHub Actions", "Shell script", "Python",
    ],

    # --- 作業系統與虛擬化（OS 層面試用） ---
    "os": [
        "systemd", "OverlayFS", "GRUB", "initramfs", "chroot", "cloud-init",
        "fsarchiver", "grub-mkrescue", "ZFS", "ext4", "LVM", "UEFI",
        "bare-metal", "ISO", "OVA", "air-gapped", "Serial Console",
    ],
    "virt": [
        "Proxmox Backup Server", "PBS", "quorum", "corosync", "live migration",
        "VMware ESXi", "KVM", "hypervisor", "snapshot", "Ceph", "iSCSI", "NFS",
    ],

    # --- 雲 ---
    "aws": [
        "AWS", "EC2", "S3", "RDS", "VPC", "EKS", "ECS", "IAM", "CloudTrail",
        "CloudFront", "KMS", "Route 53", "Lambda", "CloudFormation",
        "security group", "subnet", "availability zone",
    ],
    "gcp": [
        "GCP", "Google Cloud", "GCE", "gcloud", "Compute Engine", "GKE",
        "Cloud Storage", "service account",
    ],
    "cloud_other": ["Azure", "Aliyun", "Oracle Cloud", "OCI", "OpenTofu", "IaC"],

    # --- 可觀測性（面試很常被追問） ---
    "observability": [
        "Prometheus", "Grafana", "Alertmanager", "Loki", "exporter",
        "node_exporter", "Elasticsearch", "Kibana", "OpenTelemetry",
        "Zabbix", "Nagios", "SLI", "SLO", "SLA", "on-call",
        "incident", "root cause", "RCA", "metrics", "dashboard",
    ],

    # --- 網路與資安 ---
    "network": [
        "WireGuard", "VPN", "eBPF", "VLAN", "iptables", "nftables",
        "TCP/IP", "DNS", "CIDR", "BGP", "Cisco", "firewall",
        "load balancer", "reverse proxy", "nginx",
    ],
    "security": [
        "LDAP", "Active Directory", "PCI DSS", "air-gapped", "iRMC", "IPMI",
        "out-of-band", "encryption", "TLS", "資安", "滲透測試", "弱點掃描",
    ],

    # --- 語言與資料庫 ---
    "lang": [
        "Bash", "Golang", "Go", "Rust", "Java", "Node.js", "Next.js",
        "Swift", "Kotlin", "Flutter", "gRPC",
    ],
    "db": [
        "MySQL", "PostgreSQL", "Redis", "DynamoDB", "Kafka", "MongoDB",
        "vector database", "read replica",
    ],

    # --- 台灣求職／人資用語（HR 關與薪資談判那一關） ---
    "hr": [
        "年薪", "月薪", "年終獎金", "績效獎金", "股票選擇權", "責任制",
        "勞基法", "勞退", "勞健保", "特休", "試用期", "輪班", "駐點",
        "外派", "到職日", "職等", "面議", "加班費", "遠端工作",
    ],

    # --- 這場面試的公司（🔴 只放這場的那一家，不要整組塞；平常留空，用 --extra 也行） ---
    "companies": [
        # "範例公司", "ExampleCorp",
    ],

    # --- 你自己的專案名（讓模型別把它們拆成怪字） ---
    "mine": [
        # 自己的專案名、學校縮寫、證照（例如 "CKA"），讓模型別把它們拆成怪字
    ],
}

DEFAULT_GROUPS = ["core"]

# 超過這個 token 數就開始出現標點污染與詞彙互相稀釋（實測值，非規格）
SOFT_TOKEN_WARN = 120


def build_prompt(groups: list[str], extra: list[str] | None = None) -> str:
    """把選中的詞組串成一句 initial_prompt。

    Whisper 對「一段自然的前文」反應比對「逗號分隔的清單」好一點，
    但清單也有效且更省 token。這裡用頓號分隔，中文語境下最省。
    """
    words: list[str] = []
    seen: set[str] = set()
    for g in groups:
        if g not in GROUPS:
            raise KeyError(f"未知的詞組 `{g}`，可用：{', '.join(GROUPS)}")
        for w in GROUPS[g]:
            if w not in seen:
                seen.add(w)
                words.append(w)
    for w in extra or []:
        if w and w not in seen:
            seen.add(w)
            words.append(w)
    if not words:
        return ""
    return "以下是一段技術對話，可能出現這些專有名詞：" + "、".join(words) + "。"
