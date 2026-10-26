#!/usr/bin/env python3
# Local scan and print station for the HP LaserJet MFP M437nda.
# Configure the scanner, CUPS queue and optional printer address with STATION_*.
# Scanner drivers and CUPS configuration are deployment dependencies, not bundled here.
import os, re, subprocess, threading, datetime, glob, json, shutil, time, uuid, hmac, tempfile
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from scan_orientation import ocr_vote
except ImportError:                     # deployed without scan_orientation.py: only the optional rotation needs it
    ocr_vote = None
import office_conversion
from flask import Flask, request, send_from_directory, jsonify, Response
from werkzeug.exceptions import NotFound

SCANS = os.path.expanduser("~/scans")
JOBS = os.path.expanduser("~/printjobs")
EVENTS = os.path.join(SCANS, "events.log")
# Device addresses have no deployment-specific defaults.
DEV = os.environ.get("STATION_DEV", "")
QUEUE = os.environ.get("STATION_QUEUE", "M437")
PRINTER_IP = os.environ.get("STATION_PRINTER_IP", "")  # Optional fallback when the queue URI cannot be read.

# ---- smfp 通道（三星私有扫描协议 :9400，HP ULD 驱动的 libsane-smfp）----
# WSD/airscan 免驱但上限 200dpi（请求更高会被静默降级）；smfp 全开 75~1200dpi 且快得多
# （实测 300dpi 50mm 窗口 2.3s；WSD 任何设置都固定 ~9.5s/页）。M437 固件没有 eSCL 开关
# （SWS 全部菜单核实过），smfp 是这台机器拿到真 300/600 的唯一通道。
# 设备串的 IP 取自 `scanimage -L` 里 M437 那一行的 ip=；smfp 失败自动回退 WSD（分辨率钳回 200）。
# 依赖（容器内）：libsane-smfp.so.1 + /etc/sane.d/dll.conf 加 smfp + apt libusb-0.1-4。
# smfp scan-area limits: flatbed and feeder are both -x 0..297 -y 0..432 mm. The "feeder -x 0..254" recorded on
# 2026-09-23 was scanimage printing (br-x max - current -l) with the default -l 43 (297-43=254); re-checked with
# -l 0 --help on 2026-10-19 (0..297) and the real device delivered 297 mm wide frames.
SMFP_SRC = {"Flatbed": "Flatbed", "ADF": "ADF Simplex",
            "ADF Duplex": "ADF Duplex - Long-Edge Binding"}
SMFP_MODE = {"Color": "Color - 16 Million Colors", "Gray": "Grayscale - 256 Levels"}
ADF_MAX_W = 297.0                         # feeder takes up to 297 mm across (A3 portrait; HP spec ADF 297x432)


def smfp_device():
    """`scanimage -L` 里**打印机那一行**的 ip= → smfp 设备串；找不到返回 None（走 WSD 回退）。
    只认含 STATION_DEV 设备名(或 M437)的那一行——局域网里别的扫描仪不能被误当成它。"""
    if not DEV:
        return None
    try:
        r = subprocess.run(["scanimage", "-L"], capture_output=True, text=True, timeout=30)
        name = DEV.split(":", 2)[-1]
        for line in ((r.stdout or "") + "\n" + (r.stderr or "")).splitlines():
            if name in line or "M437" in line:
                m = re.search(r"ip=(\d+\.\d+\.\d+\.\d+)", line)
                if m:
                    return "smfp:net;" + m.group(1)
    except Exception:
        pass
    return None


RETAIN_SEC = 15 * 60
# Fingerprint of this station build: a page left open across an update sees /status.build change and reloads.
def _build_id():
    import hashlib
    h = hashlib.sha256()
    for f in ("scan_app.py", "scan_orientation.py", "office_conversion.py"):
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), f), "rb") as fh:
                h.update(fh.read())
        except OSError:
            pass
    return h.hexdigest()[:12]


BUILD_ID = _build_id()
MAX_UPLOAD = 120 * 1024 * 1024          # 单个上传上限
BLANK_INK = 0.0001                      # ink coverage < 0.01% = blank page. Lowered from 0.1% on 2026-10-19: a 4 mm
                                        # black stamp or a small signature covers only ~0.03% of A4, and the old
                                        # threshold deleted pages that carried nothing but a stamp or a signature.
                                        # 实测标定：真实内容页 5~11%，最稀疏"仅一行字" 0.50%，
                                        # 纯空白 0.0000% —— 5 倍安全余量，绝不会误删有内容的页
for d in (SCANS, JOBS):
    os.makedirs(d, exist_ok=True)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD

# 登录保护：设了 STATION_PASSWORD 才启用（不设=匿名，办公室局域网内任何人都能用）。
# 用 HTTP Basic —— 手机/电脑浏览器原生支持、能记住密码，不用自己做会话。
STATION_USER = os.environ.get("STATION_USER", "office")
STATION_PASSWORD = os.environ.get("STATION_PASSWORD", "")


@app.before_request
def _same_origin_only():
    # 员工浏览的其他网页不能替他们在打印站上删扫描件/触发扫描/打印(跨站提交)。浏览器发 POST 时都会带
    # Origin(老浏览器至少带 Referer)；主机名对不上就拒。都不带的(命令行工具)放行——那不是浏览器被利用。
    if request.method != "POST":
        return None
    from urllib.parse import urlsplit
    src = request.headers.get("Origin") or request.headers.get("Referer") or ""
    if src and src != "null" and urlsplit(src).netloc.lower() != (request.host or "").lower():
        log_event("cross-site-post", "%s from %s" % (request.path, src[:80]))
        return jsonify(error="This request did not come from the station page and was rejected"), 403
    if src == "null":
        return jsonify(error="This request did not come from the station page and was rejected"), 403
    return None


@app.before_request
def _require_length():
    # Werkzeug 2.2 对分块传输(没有 Content-Length)的请求不检查 MAX_CONTENT_LENGTH，表单字段会整个读进
    # 1 号进程的内存——一个请求就能把整站撑爆。浏览器的 fetch/FormData 永远带 Content-Length，直接拒。
    if request.method == "POST" and request.content_length is None:
        return jsonify(error="Invalid request format. Refresh the page and try again"), 411


@app.before_request
def _require_login():
    if not STATION_PASSWORD:
        return None                                  # 没配密码 = 不启用
    a = request.authorization
    # compare_digest 防时序侧信道；比较 bytes——中文用户名/密码在 str 上比较会抛 TypeError 变成 500
    if a and hmac.compare_digest((a.username or "").encode(), STATION_USER.encode()) \
            and hmac.compare_digest((a.password or "").encode(), STATION_PASSWORD.encode()):
        return None
    return Response("Login required", 401,
                    {"WWW-Authenticate": 'Basic realm="Office Scan & Print"'})


# ---- 状态：扫描仪一把锁 + 每个作业各自一条状态 ----
# ⚠️ 教训：早期全站只有一条扫描状态、一条打印状态——A 会看到 B 的"完成：scan-xxx.pdf"并当成自己的，
# B 的打印结果会顶掉 A 的失败提示。现在每个作业(扫描/打印)有自己的 id，浏览器只盯自己的作业；
# 别人在用扫描仪时只显示"Scanner in use by another user"。
office_lock = threading.Lock()                   # one Office renderer within the NAS memory budget
scan_lock = threading.Lock()                     # 物理扫描仪：谁拿到锁谁在用；busy 就是 locked()
scanner = {"pages": 0, "job": None}              # 正在进行的扫描(给别人看进度用)
_jobs = {}
_jobs_lock = threading.Lock()
JOB_KEEP_SEC = 3 * 3600


def new_job(kind, **kw):
    jid = uuid.uuid4().hex[:16]
    now = time.time()
    with _jobs_lock:
        for k in [k for k, v in _jobs.items() if now - v["t"] > JOB_KEEP_SEC]:
            del _jobs[k]
        _jobs[jid] = dict(kind=kind, state="running", msg="", file="", t=now, **kw)
    return jid


def job_update(jid, **kw):
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid].update(kw)
            _jobs[jid]["t"] = time.time()


def job_view(jid, kind):
    with _jobs_lock:
        j = _jobs.get(jid or "")
        if not j or j["kind"] != kind:
            return None
        return {k: j[k] for k in ("state", "msg", "file") if k in j}


_log_lock = threading.Lock()


def log_event(kind, msg):
    """事件落盘(可下载)——出问题后运维要能查，符合"数据必须持久化+有下载入口"的规矩。
    加锁：并发的追加和 2MB 截断交错时会丢行/写坏文件。"""
    try:
        text = re.sub(r"\b([0-9a-f]{8})[0-9a-f]{24}\b", r"\1…", str(msg))   # 打印令牌只留前 8 位：日志是公开可下载的
        line = "%s\t%s\t%s\n" % (sgt(), kind, text.replace("\n", " ")[:500])
        with _log_lock:
            with open(EVENTS, "a") as f:
                f.write(line)
            if os.path.getsize(EVENTS) > 2 * 1024 * 1024:      # 超过 2MB 只留后 3000 行
                with open(EVENTS) as f:
                    tail = f.readlines()[-3000:]
                tmp = EVENTS + ".tmp"
                with open(tmp, "w") as f:
                    f.writelines(tail)
                os.replace(tmp, EVENTS)
    except Exception:
        pass


def friendly(err):
    """把内部报错翻成行政看得懂的人话(规矩：失败要用大白话说清楚)。
    认不出来的不再把命令行/路径原样甩给用户，只给一句能照做的话，原文进事件日志。"""
    s = str(err or "")
    low = s.lower()
    table = [
        ("pdf generation timed out", "PDF generation timed out. Lower the resolution or scan in smaller batches"),
        ("device busy", "The scanner is busy. Wait 10 seconds and retry. If it remains busy, restart the printer"),
        ("no documents", "The feeder did not detect paper. Insert the stack face up, short edge first, until the printer detects it, then select Retry. For paper on the glass, select Use flatbed"),
        ("out of documents", "The feeder did not detect paper. Insert the stack face up, short edge first, until the printer detects it, then select Retry. For paper on the glass, select Use flatbed"),
        ("jam", "Paper jam. Remove the jammed paper, then scan again"),
        ("cover open", "The printer cover is open. Close it and retry"),
        ("cover is open", "The scanner or feeder cover is open. Close it and retry"),
        # "open of device … Invalid argument" 是打不开扫描仪，必须排在 invalid argument 前面(F32)
        ("open of device", "Cannot reach the scanner. Check printer power and its network cable"),
        ("no such device", "Cannot reach the scanner. Check printer power and its network cable"),
        ("invalid argument", "The scanner does not support these settings. Choose another resolution or size"),
        ("operation not supported", "The scanner does not support this operation. Change the settings and retry"),
        ("access to resource has been denied", "Scanner access was denied. Another computer may be using it; wait and retry"),
        ("out of memory", "This job exceeds available memory. Lower the resolution or split the job"),
        ("error during device i/o", "Scanner communication stopped. Check printer power and its network cable, then retry"),
        ("incorrect password", "This PDF is encrypted. Save an unencrypted copy before uploading"),
        ("encrypt", "This PDF is encrypted. Save an unencrypted copy before uploading"),
        ("may not be a pdf", "Cannot open this file. It may be damaged or not a valid PDF"),
        ("syntax error", "Cannot open this file. It may be damaged or not a valid PDF"),
        ("cannot identify image", "Cannot open this image. It may be damaged or use an unsupported format"),
        ("connection", "Cannot reach the printer. Check its power and network cable"),
        ("timed out", "The operation timed out. Wake the printer using its panel, then retry"),
        ("timeout", "The operation timed out. Wake the printer using its panel, then retry"),
        ("no space", "Station storage is full. Contact the administrator"),
        ("cannot allocate memory", "This job exceeds station memory. Lower the resolution or split the job"),
        ("memoryerror", "This job exceeds station memory. Lower the resolution or split the job"),
        ("already completed", "This job has already been sent and cannot be cancelled"),       # CUPS: "Job #29 is already completed - can't cancel."
        ("already canceled", "This job has already been cancelled"),
        ("already aborted", "This job has already ended and cannot be cancelled"),
        ("can't cancel", "This job has already been sent and cannot be cancelled"),
        ("cancel is not possible", "This job has already been sent and cannot be cancelled"),
        ("permission", "Permission denied. Contact the administrator"),
        ("no such file or directory", "A required program or file is missing. Contact the administrator with the time of the error"),
    ]
    for k, v in table:
        if k in low:
            return v
    if not s:
        return "Something went wrong. Retry, or contact the administrator if it persists"
    if ("exit status" in low and "command" in low) or re.search(r"(^|[\s'\"(=:])/[\w.-]+/", s):   # 命令行/路径(含带引号的)不给用户看
        return "Processing failed. Retry, or contact the administrator with the error time if it persists"
    return s[:120] + " (If this persists, send this message to the administrator)"


def queue_printer_ip():
    """打印队列实际发往的打印机地址(lpstat -v 的 device-uri)。读不到才用 STATION_PRINTER_IP 兜底——
    预检必须测"队列真正要连的那台"，不能测一个可能早就过时的环境变量。"""
    try:
        out = subprocess.run(["lpstat", "-v", QUEUE], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"://(?:[^@/\s]*@)?\[?([0-9A-Za-z.\-:]+?)\]?(?::\d+)?(?:/|\s|$)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return PRINTER_IP


def printer_online():
    """打印机是否真的在线。CUPS 队列会照收作业不报错，打印机关着也"成功"——
    那就是把失败伪装成成功，必须在提交前先验。返回 (在线?, 地址)。"""
    # 用 TCP 连打印口，不用 ping —— 容器里 ICMP 常被限制(无 NET_RAW)，
    # 拿 ping 判定会把好好的打印机误报成"Unreachable"(实测踩过)。
    import socket
    ip = queue_printer_ip()
    for port in (9100, 631):
        try:
            with socket.create_connection((ip, port), timeout=3):
                return True, ip
        except OSError:
            continue
    return False, ip


_pstate = {"online": None, "ip": "", "t": 0.0, "probing": False}
PRINTER_PROBE_SEC = 60


def printer_status():
    """页头"打印机在线/连不上"用的缓存状态。只在有人开着页面(轮询 /status)时才探测，
    且最多每分钟一次、在后台线程里做——不拖慢 /status，也不会一直去敲打印机把它从休眠里叫醒。"""
    if time.time() - _pstate["t"] > PRINTER_PROBE_SEC and not _pstate["probing"]:
        _pstate["probing"] = True

        def probe():
            try:
                ok, ip = printer_online()
                _pstate.update(online=ok, ip=ip)
            finally:
                _pstate.update(t=time.time(), probing=False)
        threading.Thread(target=probe, daemon=True).start()
    return {"online": _pstate["online"], "ip": _pstate["ip"]}


def _cups_job_state(cups_id):
    """作业在 CUPS 里的最终结局：'completed' / 'canceled' / 'aborted'，查不到返回 ''。
    ⚠️ `lpstat -W completed` 把取消的、中止的作业也列进"已完成"，只看它会把取消/失败报成成功——
    用 ipptool 问作业本身的 job-state 才准(2026-09-23 容器内实测可用)。"""
    num = (cups_id or "").rsplit("-", 1)[-1]
    if not num.isdigit():
        return ""
    try:
        r = subprocess.run(["ipptool", "-tv", "ipp://localhost/jobs/%s" % num,
                            "/usr/share/cups/ipptool/get-job-attributes.test"],
                           capture_output=True, text=True, timeout=15)
        m = re.search(r"job-state \(enum\) = (\S+)", r.stdout or "")
        return m.group(1) if m else ""
    except Exception:
        return ""


def _job_state(jid):
    with _jobs_lock:
        return (_jobs.get(jid) or {}).get("state", "")


FILTER_FAIL_MARKS = ("filter failed", "filter errors", "job-completed-with-errors", "cups-filter-crashed")


def _job_block(text, cups_id):
    """`lpstat -l -o` 里某个作业自己的那几行(Status:/Alerts:)。"""
    out, inside = [], False
    for line in (text or "").splitlines():
        if line and not line[0].isspace():
            inside = line.split()[0] == cups_id
        elif inside:
            out.append(line)
    return "\n".join(out)


def _job_ids(text):
    """lpstat 输出里作业那一行(不缩进)的第一个字段是作业号。按整词比较，不做子串匹配。"""
    return {line.split()[0] for line in (text or "").splitlines() if line.strip() and not line[0].isspace()}


def watch_print_job(pjid, cups_id, desc, sheets):
    """后台盯作业真实结局，只更新**这个作业自己**的状态。
    ⚠️ 队列是 socket://9100(raw)：CUPS 标"completed"只代表数据已全部送进打印机，不代表纸已出完——
    所以成功文案是"Printer received the job全部内容"，不说"打印完成"（CUPS 收下 ≠ 已出纸）。"""
    deadline = time.time() + 120 + 4 * max(1, sheets)   # 22 页/分钟 ≈ 2.7 秒/张，给足余量
    blocked, errors, polls = "", 0, 0
    while time.time() < deadline:
        time.sleep(4)
        if _job_state(pjid) == "cancelled":               # 用户在页面上取消了——结局已定，别再改写
            return
        try:
            qr = subprocess.run(["lpstat", "-l", "-o", QUEUE], capture_output=True, text=True, timeout=15)
            if qr.returncode != 0:
                # lpstat 自己失败(cupsd 忙/重启)时输出是空的——不能当成"作业已离开队列"去下结论
                raise RuntimeError("lpstat -o failed: %s" % (qr.stderr or "").strip()[:100])
            q = qr.stdout
            ids = [line.split()[0] for line in q.splitlines() if line.strip() and not line[0].isspace()]
            if ids and ids[0] != cups_id and cups_id in ids:
                deadline += 4                                 # 排在别人的作业后面：等待时间不算"慢"
            if cups_id not in _job_ids(q):                    # 不在队列里 = 已结束(送完/取消/中止)
                outcome = _cups_job_state(cups_id)
                if not outcome:                               # ipptool 不可用时退回 lpstat(分不清取消)
                    done = subprocess.run(["lpstat", "-W", "completed", "-o", QUEUE],
                                          capture_output=True, text=True, timeout=15).stdout
                    outcome = "completed" if cups_id in _job_ids(done) else ""
                if _job_state(pjid) == "cancelled":
                    return
                if outcome == "completed":
                    job_update(pjid, state="done",
                               msg="The printer received all data (%s). Check the printer and collect the pages" % desc)
                    log_event("print-done", cups_id)
                elif outcome == "canceled":
                    job_update(pjid, state="cancelled",
                               msg="Job %s was cancelled at the printer or elsewhere. Remaining pages will not print" % cups_id)
                    log_event("print-canceled", cups_id)
                elif not outcome:                             # 查不到结局：说实话，别劝人重打(可能重复出纸)
                    job_update(pjid, state="unknown",
                               msg="Cannot determine the result of job %s. Check for printed pages before retrying" % cups_id)
                    log_event("print-unknown", cups_id)
                else:
                    job_update(pjid, state="error",
                               msg="Error: Print job %s was aborted. Check the printer panel, then retry" % cups_id)
                    log_event("print-lost", "%s %s" % (cups_id, outcome or "unknown"))
                return
            st = subprocess.run(["lpstat", "-l", "-p", QUEUE], capture_output=True, text=True, timeout=15).stdout
            low = st.lower() + "\n" + _job_block(q, cups_id).lower()     # 队列状态 + 这个作业自己的状态行
            now_blocked = ""
            polls += 1
            block = _job_block(q, cups_id).lower()
            if (any(k in block for k in FILTER_FAIL_MARKS)
                    or (polls % 5 == 0 and _cups_job_state(cups_id) == "processing-stopped")):
                # CUPS 2.4：过滤器出错的作业停在 processing-stopped，永远不会自己打出来，还一直占着队列
                # (retry-job 只管后端出错)。取消它，并告诉用户是文件的问题，别让人去找没纸/卡纸。
                subprocess.run(["cancel", cups_id], capture_output=True, text=True, timeout=20)
                job_update(pjid, state="error",
                           msg="Error: The print filter could not process this file. Export a new PDF from its original application and retry")
                log_event("print-filter-failed", "%s %s" % (cups_id, block.strip()[:150]))
                return
            for k, msg in (("disabled", "The print queue is stopped"), ("paused", "The printer is paused"),
                           ("out of paper", "The printer is out of paper"), ("media-empty", "The printer is out of paper"),
                           ("jam", "The printer has a paper jam"), ("cover", "The printer cover is open"), ("door-open", "The printer cover is open"),
                           ("toner-empty", "The printer is out of toner")):
                if k in low:
                    now_blocked = msg
                    break
            if now_blocked and now_blocked != blocked:
                job_update(pjid, state="blocked",
                           msg="Error: %s. Your job remains queued and will resume after the problem is resolved" % now_blocked)
                log_event("print-blocked", "%s %s" % (cups_id, now_blocked))
            elif blocked and not now_blocked:               # 问题解决了 → 状态恢复，不能一直挂着旧的报错
                job_update(pjid, state="running", msg="Printing (%s)..." % desc)
            blocked = now_blocked
            errors = 0
        except Exception as e:
            errors += 1
            if errors >= 3:
                job_update(pjid, state="unknown",
                           msg="Cannot track job %s. Check the printer for output" % cups_id)
                log_event("print-watch-error", "%s %s" % (cups_id, e))
                return
    job_update(pjid, state="slow",
               msg="Job %s has been waiting too long. Check the printer panel for missing paper or a jam" % cups_id)
    log_event("print-slow", cups_id)


def reap_orphans():
    """容器里本进程是 1 号进程(run.sh 最后 exec python)，孤儿进程(LibreOffice 起的 gpgconf/gpg/gpgsm 等)
    都会过继给它——Python 不会自动回收，2026-09-23 实测 1.8 天攒了 45 个僵尸，容器 pids 上限 512，
    攒满后连 scanimage/lp 都起不来，整站瘫痪。
    安全做法：只回收**连续两轮(≥30 秒)都是僵尸**的子进程——我们自己用 subprocess 起的子进程
    一退出就会被 subprocess 立刻 wait 掉，不可能挂 30 秒，所以不会跟 subprocess 抢返回码。"""
    if os.getpid() != 1:
        return
    seen = set()
    while True:
        time.sleep(30)
        try:
            zombies = set()
            for d in os.listdir("/proc"):
                if not d.isdigit():
                    continue
                try:
                    with open("/proc/%s/stat" % d) as fh:
                        fields = fh.read().rsplit(")", 1)[1].split()
                except OSError:
                    continue
                if fields[0] == "Z" and fields[1] == "1":
                    zombies.add(int(d))
            for pid in zombies & seen:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
            seen = zombies
        except Exception as e:
            log_event("reaper-error", e)


def postprocess_page(png_path, dpi, do_orient, do_enhance, rotation=None):
    """一次加载、一次保存里做完 转正 + 去灰底。
    ⚠️ 性能教训：早期"裁切→转正→增强"各自独立读写，一页 200dpi 的 A3 要**存 3 次全分辨率 PNG**，
    用户直接反馈"太慢了"。合并后每页只存 1 次；OSD 也改用缩略图判方向(方向判断不需要全分辨率)。
    ⚠️ 内存教训(2026-09-23 实测)：整页 float32 数组 + 一串临时数组，一页 600dpi A3 彩色峰值 6.1GB，
    容器上限 2GB → 整站被 OOM 杀掉。现在按行分条(_enhance_inplace)，峰值 ~1.0GB，输出逐像素不变。
    rotation: a direction already decided for the whole batch (decide_orientations); when given, no per-page OSD runs.
    返回 (转了多少度, 是否增强过, 出错说明列表)——出错不再静默吞掉，由调用方如实告诉用户。"""
    from PIL import Image
    problems = []
    im = Image.open(png_path)
    im.load()
    rot = 0
    if do_orient:
        try:
            rot = _osd_degrees(im) if rotation is None else rotation
            if rot:
                op = {90: Image.ROTATE_270, 180: Image.ROTATE_180,
                      270: Image.ROTATE_90}.get(rot)
                if op:
                    im = im.transpose(op)               # 整数旋转，无重采样不损画质
                else:
                    rot = 0
        except Exception as e:
            rot = 0
            problems.append("Orientation correction failed: %s" % e)
    enhanced = False
    if do_enhance:
        try:
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB" if im.mode in ("RGBA", "P", "CMYK", "YCbCr", "LAB", "HSV") else "L")
            enhanced = _enhance_inplace(im)
        except Exception as e:
            problems.append("Text enhancement failed: %s" % e)
    if rot or enhanced:
        im.save(png_path, dpi=(dpi, dpi))
    return rot, enhanced, problems


ENHANCE_STRIP = 256     # 每条的行数；必须是 8 的倍数——条内 [::8]/[::4] 抽样才等于整页抽样(结果逐像素不变)


def _enhance_inplace(im):
    """就地做 ① 纸白白平衡 ② 按色度加权的亮度曲线。im 为 RGB 或 L；返回是否改动过。
    ① 纸白白平衡：smfp 通道实测纸底 G 比 R/B 低 ~5 灰阶（偏品红，用户报"发红/不是白纸"）。
       按各通道纸白点(P90)等比对齐——等同相机白平衡，色相关系整体保持。与曾被用户否掉的
       "逐通道 levels 拉伸"不同：只乘小系数(封顶 1.08)、不动黑场，不会出怪色。
       门槛：纸白点都够亮(>180，深色照片不动) 且通道差 >1.5 灰阶才校正。
    ② 亮度曲线：转 YCbCr 只动 Y(亮度)，且**只作用在接近灰色的像素**上。
       ⚠️ 教训：一开始对所有像素压亮度，红印章(205,45,45)被压成(149,0,0)、
       蓝签名被压成(0,10,139)——饱和色的通道被裁到 0 就变色了。
       现在按色度加权：灰的(文字/纸)才加深，有颜色的(印章/签名/照片)原样不动。"""
    import numpy as np
    from PIL import Image
    W, H = im.size
    S = ENHANCE_STRIP

    def strip(y0):
        return (0, y0, W, min(H, y0 + S))

    if im.mode == "L":                          # 灰度：曲线就是逐值映射 → 256 项查找表，不建整页浮点数组
        s4 = [np.asarray(im.crop(strip(y0)))[::4, ::4].copy() for y0 in range(0, H, S)]
        r = _lum_curve(np.concatenate(s4).astype(np.float32))
        if r is None:
            return False
        black, white = r
        lut = np.clip((np.arange(256, dtype=np.float32) - black) * (255.0 / (white - black)), 0, 255)
        im.paste(im.point(lut.astype("uint8").tolist()))
        return True
    # 第一遍：只收集抽样(复制出来——留着视图会把每一条整条钉在内存里)
    s8, s4 = [], []
    for y0 in range(0, H, S):
        a = np.asarray(im.crop(strip(y0)))
        s8.append(a[::8, ::8].reshape(-1, 3).copy())
        s4.append(a[::4, ::4].copy())
    wp = np.percentile(np.concatenate(s8).astype(np.float32), 90, axis=0)
    scale = None
    if wp.min() > 180 and (wp.max() - wp.min()) > 1.5:
        scale = np.minimum(wp.max() / np.maximum(wp, 1.0), 1.08)     # float64：改 float32 会改变取整结果
    ys = []                                     # 白平衡后的亮度抽样，逐条算
    for c in s4:
        if scale is not None:
            c = np.clip(c.astype(np.float32) * scale, 0, 255).astype("uint8")
        ys.append(np.asarray(Image.fromarray(c, "RGB").convert("YCbCr"))[:, :, 0].copy())
    del s8, s4
    r = _lum_curve(np.concatenate(ys).astype(np.float32))
    if scale is None and r is None:
        return False
    # 第二遍：逐条处理后贴回原图
    for y0 in range(0, H, S):
        box = strip(y0)
        blk = np.asarray(im.crop(box), dtype=np.float32)
        if scale is not None:
            blk = np.clip(blk * scale, 0, 255)
        b8 = blk.astype("uint8")
        if r is not None:
            black, white = r
            ycc = np.asarray(Image.fromarray(b8, "RGB").convert("YCbCr"), dtype=np.float32)
            ya = ycc[:, :, 0]
            chroma = np.maximum(np.abs(ycc[:, :, 1] - 128.0), np.abs(ycc[:, :, 2] - 128.0))
            w = np.clip((18.0 - chroma) / 10.0, 0.0, 1.0)     # 色度越低权重越高，平滑过渡不产生硬边
            curved = np.clip((ya - black) * (255.0 / (white - black)), 0, 255)
            ycc[:, :, 0] = ya * (1.0 - w) + curved * w
            b8 = np.asarray(Image.fromarray(ycc.astype("uint8"), "YCbCr").convert("RGB"))
        im.paste(Image.fromarray(b8, "RGB"), box[:2])
    return True


OSD_CAP = 2200          # OSD 输入图的长边上限，见下方实测表

def _osd_once(im, cap):
    """跑一次 tesseract OSD，返回 (需转角度, 置信度)；失败返回 (0, 0.0)。"""
    work = im
    if cap and max(im.size) > cap:
        work = im.copy()
        work.thumbnail((cap, cap))
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        work.convert("L").save(tmp)
        r = subprocess.run(["tesseract", tmp, "-", "--psm", "0", "-l", "osd"],
                           capture_output=True, text=True, timeout=120)
    except Exception:
        return 0, 0.0
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    deg, conf = 0, 0.0
    for line in (r.stdout or "").splitlines():
        if line.startswith("Rotate:"):
            try:
                deg = int(line.split(":")[1].strip())
            except ValueError:
                return 0, 0.0
        elif line.startswith("Orientation confidence:"):
            try:
                conf = float(line.split(":")[1].strip())
            except ValueError:
                conf = 0.0
    return deg % 360, conf


def _osd_degrees(im, min_conf=3.0):
    """Tesseract OSD 判方向（它认中文 Han）。先用 2200px 缩略图，判不出再用全分辨率重试。
    实机实证：ADF 扫出来的合同页全是 `Rotate: 90`（整页侧躺，置信度 16~22）；只做 90/180/270
    整数旋转（无损），置信度 <3 不动。
    ⚠️ 实测教训（缩得太小会**静默失效**，一张侧躺页就这么漏过去了）——
    同一张 2339x3307 的真实侧躺页，OSD 结果随输入尺寸：
      1000px → 需转 0°  conf 0.00  ✗ 完全认不出
      1400px → 需转90°  conf 3.95  勉强
      1800px → 需转90°  conf11.58  ✓
      2200px → 需转90°  conf16.15  ✓ 1.88s
      全分辨率 → 需转90° conf17.24  ✓ 2.19s
    缩到 1000px 只省 0.3 秒却完全失效，得不偿失。
    注：曾自写过基于文字行 minAreaRect 的小角度摆正，实测正的页面被乱转、
    人为转歪的又纠不回来，已废弃——半吊子的自动摆正比不做更糟。"""
    deg, conf = _osd_guess(im, min_conf)
    return deg if (deg and conf >= min_conf) else 0


def _osd_guess(im, min_conf=3.0):
    """(clockwise degrees to turn upright, confidence). Thumbnail first; full resolution only when the thumbnail is
    NOT confident. (An early version retried whenever the answer was 0 deg, so every upright page - the common case -
    ran a second full-size OSD, doubling time and memory.)"""
    deg, conf = _osd_once(im, OSD_CAP)
    if conf < min_conf and max(im.size) > OSD_CAP:
        d2, c2 = _osd_once(im, None)
        if c2 >= conf:
            deg, conf = d2, c2
    return deg % 360, conf


RED_INK_MIN = 20        # >= this many red pixels on the 1/4 thumbnail = a red seal (even a thin cross-page seal
                        # slice has hundreds), so the page is not blank


def _red_pixels(im):
    """Red pixels on the 1/4 thumbnail (R at least 40 above both G and B). Red seals are not dark, so the
    darkness-based ink coverage does not count them."""
    import numpy as np
    w, h = im.size
    a = np.asarray(im.convert("RGB").resize((max(1, w // 4), max(1, h // 4))), dtype=np.int16)
    return int(((a[..., 0] - np.maximum(a[..., 1], a[..., 2])) >= 40).sum())


def _ink_ratio_im(im):
    """Same as ink_ratio, on an image that is already open (the batch orientation pass reads each page once)."""
    import numpy as np
    w, h = im.size
    s = np.asarray(im.convert("L").resize((max(1, w // 4), max(1, h // 4))), dtype=np.float32)
    white = float(np.percentile(s, 95))
    return 1.0 if white < 200 else float((s < white - 40).mean())


ORIENT_SURE = 3.0       # OSD confidence that counts as sure (measured: normal pages 10-23, sparse signature pages 0.07-5)
ORIENT_OWN_VETO = 2.0   # a page whose own OSD is at least this sure and disagrees keeps out of the batch direction


def decide_orientations(pages):
    """Decide page directions for a whole batch. Returns [(clockwise degrees, note for the user, error or "")],
    in page order. Only runs when the user explicitly asks for rotation.
    1. Blank pages (no ink, no red seal) are skipped; they used to run OSD plus four-way OCR and get reported as
       "rotation failed".
    2. Per-page OSD; a sure answer is used as is.
    3. A page that is not sure takes the direction of the sure pages of the SAME shape (all portrait or all
       landscape) in the batch when those all agree - a stack is normally loaded one way. 2026-10-19: page 1 of a
       contract had OSD 13.7, its sparse signature page only 0.07-1.58 and stayed sideways.
    4. Only then ocr_vote compares recognised Chinese/English text in four directions; it never contradicts a
       weak-but-stated OSD direction.
    5. Still unsure: the page stays as scanned with a plain note - not an error, nothing is missing."""
    from PIL import Image
    infos = []
    for p in pages:
        try:
            with Image.open(p) as im:
                im.load()
                if _ink_ratio_im(im) < BLANK_INK and _red_pixels(im) < RED_INK_MIN:
                    infos.append({"blank": True})
                    continue
                deg, conf = _osd_guess(im, ORIENT_SURE)
                infos.append({"deg": deg % 360, "conf": conf, "land": im.width > im.height})
        except Exception as e:
            infos.append({"error": "%s: %s" % (type(e).__name__, e)})
    sure = [i for i in infos if "conf" in i and i["conf"] >= ORIENT_SURE]
    out = []
    for n, i in enumerate(infos, 1):
        if i.get("blank"):
            out.append((0, "", ""))
            continue
        if "error" in i:
            out.append((0, "", "Orientation correction failed: " + i["error"][:150]))
            continue
        if i["conf"] >= ORIENT_SURE:
            out.append((i["deg"], "", ""))
            continue
        same = {j["deg"] for j in sure if j["land"] == i["land"]}
        if len(same) == 1 and not (i["conf"] >= ORIENT_OWN_VETO and i["deg"] not in same):
            rot = same.pop()
            out.append((rot, "Rotated to match other pages in this batch" if rot else "", ""))
            continue
        try:
            if ocr_vote is None:
                raise RuntimeError("scan_orientation.py is not deployed")
            with Image.open(pages[n - 1]) as im:
                im.load()
                rot, detail = ocr_vote(im, (i["deg"], i["conf"]))
        except Exception as e:
            rot, detail = None, "ocr error %s: %s" % (type(e).__name__, str(e)[:80])
        log_event("scan-orient-uncertain", "p%d osd=%d/%.2f %s" % (n, i["deg"], i["conf"], detail))
        if rot is None:
            out.append((0, "Orientation uncertain; kept the original", ""))
        else:
            out.append((rot, "Rotated using the direction of the page text" if rot else "", ""))
    return out


def _lum_curve(y_small):
    """从**亮度**算一条温和的 levels 曲线：白场提到纸白、黑场适度下压让字更实。
    返回 (black, white) 或 None(不需要处理)。
    ⚠️ 两条实测教训：
      ① 用户反馈"颜色很不正常/不自然"——起因是我按 R/G/B **逐通道**拉伸，会改变通道比例产生偏色。
         现在只处理亮度(Y)、色度(Cb/Cr)原样保留，**色相和饱和度数学上完全不变**。
      ② 别把黑场压到 0。之前取 2% 分位当黑场，字被压死、印章发死黑，就是"效果过了"。
         现在黑场只取暗部分位数的一半，且总增益封顶 1.6，属于"能看出变实但不假"。"""
    import numpy as np
    white = float(np.percentile(y_small, 97))
    dark = float(np.percentile(y_small, 5))
    if white < 255.0 / 1.6 or float(np.median(y_small)) < 255.0 / 1.6:   # 整页偏暗 → 不动
        # 中位数也要看：深色幻灯片上的浅色字会把 P97 抬到 160 以上，但页面大部分是深色底，不是"白纸灰字"
        # ⚠️ 早期门槛是 60：白点在 60~160 的深色页黑场压到 0 后增益仍高达 1.6~4 倍，
        #    "增益封顶 1.6"形同虚设，深色页被提成一片灰白(C01)。现在白点不到 160 就不是"白纸灰字"，不碰。
        return None
    black = max(0.0, min(dark * 0.5, white - 160))    # 温和黑场，且保证动态范围不过窄
    if white - black < 1:
        return None
    gain = 255.0 / (white - black)
    if gain > 1.6:                                    # 增益封顶，避免"过了"
        black = max(0.0, white - 255.0 / 1.6)
    if 255.0 / max(1.0, white - black) <= 1.03:       # 本来就够好 → 不动
        return None
    return black, white


def ink_ratio(png_path):
    """墨迹覆盖率：明显比纸白暗的像素占比。用于空白页判定。"""
    from PIL import Image
    import numpy as np
    im = Image.open(png_path).convert("L")
    w, h = im.size
    s = np.asarray(im.resize((max(1, w // 4), max(1, h // 4))), dtype=np.float32)
    white = float(np.percentile(s, 95))                  # 纸白基准(抗污点/黑边)
    if white < 200:
        # 不是白纸(深色封面/深色幻灯片)：以纸白为基准的算法在这里失灵，会把有内容的深色页当空白删掉(C02)。
        # 这种页一律当"有内容"。
        return 1.0
    return float((s < white - 40).mean())


def drop_blank_pages(pages, workdir):
    """剔除空白页。返回 (保留的页, 剔除数)。全是空白时一页不删(宁可留着也不交空 PDF)。"""
    from PIL import Image
    keep, dropped = [], 0
    for p in pages:
        try:
            blank = ink_ratio(p) < BLANK_INK
            if blank:                                    # red seals are not dark: a page with only a stamp/seal slice is not blank
                with Image.open(p) as im:
                    blank = _red_pixels(im) < RED_INK_MIN
        except Exception:
            blank = False                                # 判不了就当有内容，绝不误删
        if blank:
            dropped += 1
        else:
            keep.append(p)
    if not keep:
        return pages, 0
    return keep, dropped


def run_group(cmd, timeout, **kw):
    """同 subprocess.run(capture_output=True, text=True)，但子进程自成一组；超时把**整组**杀掉。
    ⚠️ 只杀父进程时，ocrmypdf 的 tesseract 工作进程、unoconv 起的 soffice 监听会变成孤儿——
    占着内存/端口，之后每次 Office 转换都先卡满 120 秒超时。"""
    import signal
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         start_new_session=True, **kw)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.communicate()
        raise
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def ocr_pdf(pdf_path):
    """就地做中文+英文 OCR，生成可搜索 PDF。失败返回错误原因(不静默吞)。"""
    out = pdf_path + ".ocr.pdf"
    try:
        # --jobs 2：容器只有 3 个 CPU、2GB 内存，4 个 tesseract 并发跑 600dpi 页会把内存吃光
        r = run_group(["ocrmypdf", "-l", "chi_sim+eng", "--output-type", "pdf",
                       "--optimize", "0", "--force-ocr", "--jobs", "2",
                       "--quiet", pdf_path, out], timeout=900)
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            shutil.move(out, pdf_path)
            return None
        return (r.stderr or "OCR failed").strip()[:200]
    except subprocess.TimeoutExpired:
        return "OCR timed out"
    except Exception as e:
        return str(e)[:200]
    finally:
        if os.path.exists(out):
            os.remove(out)

# 分辨率档位按 smfp 通道给（75~1200 全支持）；1200 不提供——A3 彩色一页 ~830MB，
# NAS 只有 8GB 内存，后处理会被打爆。WSD 回退时 >200 的请求会被钳回 200，
# 且 run_scan 里以设备返回的真实 DPI 标注（谁降级都不会再把几何算错）。
RES = ["100", "150", "200", "300"]          # 300 dpi maximum (owner, 2026-10-19); 600 removed
SOURCES = {"Flatbed": "Flatbed (place face down on the glass)", "ADF": "Feeder, single-sided", "ADF Duplex": "Feeder, double-sided"}
MODES = {"Color": "Color", "Gray": "Grayscale"}
PAPERS = {"A4": "A4", "Letter": "Letter", "A3": "A3", "A5": "A5", "Legal": "Legal"}
SIDES = {"None": "Single-sided", "DuplexNoTumble": "Double-sided, long edge (LEF)", "DuplexTumble": "Double-sided, short edge (SEF)"}

# 这台 M437nda 扫描台最大幅面 = A3 (297x420mm)；不给几何参数 scanimage 就出 A3
BED_W, BED_H = 297.0, 420.0
# ADF 扫描窗口每侧额外多要的毫米数。⚠️ 2026-08-25 骑缝章事故：驱动默认 -l 43 -x 210 让窗口
# 与 A4 纸严丝合缝，进纸稍偏 1~2mm 纸缘就被裁掉——7 页合同只有Page 1保住了骑缝章，其余页
# 盖在纸缘那几毫米的红章全没了。现在窗口比纸宽 2×6mm，扫完由 trim_adf_side_margins 裁掉
# 纸外区域再居中吸附回标准尺寸，纸缘内容不再丢。
ADF_EDGE_MARGIN = 6.0
# 进纸道中心：驱动官方默认 A4 窗口 -l 43 -x 210 → 纸在 43..253mm，中心 148mm。加宽窗口必须以它为中心
# 对称展开(A4 → -l 37 -x 222)；早期按 297/2=148.5 算再四舍五入成 -l 38，两边纸外只剩 5/7mm，不对称。
ADF_CENTER_MM = 148.0
STD_SIZES = [("A6", 105, 148), ("A5", 148, 210), ("B5", 176, 250), ("A4", 210, 297),
             ("Letter", 216, 279), ("Legal", 216, 356), ("A3", 297, 420)]
# Since 2026-10-19 every scan is the scanner's own output: no cropping, rotating, darkening or page removal at scan
# time. The old "auto detect and crop" size was removed; processing like that may only be an explicit choice made
# after the user has seen the scan.
SCAN_SIZES = {"full": "Full area (when the paper size is unknown)", "A4": "A4", "A5": "A5", "B5": "B5",
              "Letter": "Letter", "Legal": "Legal", "A3": "A3"}


SGT = datetime.timezone(datetime.timedelta(hours=8))


def sgt():
    return datetime.datetime.now(SGT).strftime("%Y%m%d-%H%M%S")      # 新加坡时间(UTC+8)


def snap_size(w_mm, h_mm, tol=4.0):
    """吸附到能装下的最小标准尺寸(允许横放)。装不下返回 None。"""
    best = None
    for name, sw, sh in STD_SIZES:
        for pw, ph in ((sw, sh), (sh, sw)):
            if w_mm <= pw + tol and h_mm <= ph + tol:
                if best is None or pw * ph < best[0]:
                    best = (pw * ph, name, pw, ph)
    return best[1:] if best else None


def snap_up_size(w_mm, h_mm, slack=14.0):
    """裁完后"差一点点就是标准纸"时，补白边还原成标准尺寸。
    ⚠️ 实测场景：用户一次 4 页扫描里有一页是 **209.7×284.5mm**——宽度正好 A4(210)，
    高度短 12mm，因为 A4 页下方的空白页边被当成设备填充裁掉了，结果尺寸非标、看着"不准"。
    规则：能被某标准尺寸装下，且**两边富余都 ≤slack(14mm)**，就补白边还原（209.7×284.5 → A4）。
    ⚠️ slack 不能放大到 20mm：实测 260×209 的非标页会被硬套成 Letter(279×216) 贴错标签——
    这正是 nearest_size 当初要避免的问题。14mm 刚好救得回被裁白边的 A4，又挡得住乱套。"""
    best = None
    w_mm, h_mm = round(w_mm, 1), round(h_mm, 1)       # 量出的毫米数带像素取整误差(283mm 量成 282.956)，按 0.1mm 比
    for name, sw, sh in STD_SIZES:
        for pw, ph in ((sw, sh), (sh, sw)):
            dw, dh = pw - w_mm, ph - h_mm
            # -1.5mm：量出来的宽按整列像素算，210mm 的纸会量成 210.06mm(差一列)——不能因此认不回 A4
            if -1.5 <= dw <= slack and -1.5 <= dh <= slack:
                if best is None or pw * ph < best[0]:
                    best = (pw * ph, name, pw, ph)
    return best[1:] if best else None


def nearest_size(w_mm, h_mm, tol=6.0):
    """只在**两边都接近**某标准尺寸时才吸附（|差|<=tol），否则返回 None → 保留实测尺寸。
    与 snap_size("能装下的最小标准尺寸")不同：ADF 实测出 260x209mm 这种非标尺寸时，
    snap_size 会硬套成 Letter(279x216) 贴个错标签；如实保留 260x209 才是诚实的做法。"""
    for name, sw, sh in STD_SIZES:
        for pw, ph in ((sw, sh), (sh, sw)):
            if abs(w_mm - pw) <= tol and abs(h_mm - ph) <= tol:
                return name, pw, ph
    return None


def _largest_run(flags):
    """最长连续 True 段 (start, end)；没有返回 None。"""
    best, cur = None, None
    for i, v in enumerate(list(flags) + [False]):
        if v and cur is None:
            cur = i
        elif not v and cur is not None:
            if best is None or i - cur > best[1] - best[0]:
                best = (cur, i)
            cur = None
    return best


def detect_rotated(png_path, dpi):
    """OpenCV 旋转矩形检测：能认出**放歪的**纸并给出角度。
    返回 (cx, cy, w_mm, h_mm, angle_deg)；失败返回 None。"""
    import cv2
    import numpy as np
    g = cv2.imread(png_path, cv2.IMREAD_GRAYSCALE)
    if g is None:
        return None
    H, W = g.shape
    scale = 900.0 / max(H, W)                       # 缩小加速，精度足够(误差<1mm)
    small = cv2.resize(g, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    h, w = small.shape
    bh, bw = max(1, int(h * .08)), max(1, int(w * .08))
    bg = float(np.median(small[h - bh:, w - bw:]))  # 远角=空台面基准
    sigma = float(np.std(small[h - bh:, w - bw:]))
    diff = cv2.absdiff(small, np.full_like(small, int(round(bg))))
    diff = cv2.GaussianBlur(diff, (5, 5), 0)
    # 门槛随背景噪声自适应：太低会把噪点/正文当纸(实测 σ=8 时固定门槛必崩)
    _, m = cv2.threshold(diff, max(4.0, sigma * 4), 255, cv2.THRESH_BINARY)
    # 闭运算前先在四周垫一圈背景再裁回：否则贴着台面边角的纸，闭运算会把边界外当成纸往外"长"，
    # 歪放的 A4 被量大 4~6mm 当成 Legal 裁出来(F27)
    P = 20
    m = cv2.copyMakeBorder(m, P, P, P, P, cv2.BORDER_CONSTANT, value=0)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8), iterations=2)[P:-P, P:-P]
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    if cv2.contourArea(c) < h * w * 0.02:           # 太小 = 噪点
        return None
    (cx, cy), (rw, rh), ang = cv2.minAreaRect(c)
    if rw < 5 or rh < 5:
        return None
    # minAreaRect 角度归一到 [-45,45]，方便判断是否需要摆正
    if ang > 45:
        ang -= 90; rw, rh = rh, rw
    elif ang < -45:
        ang += 90; rw, rh = rh, rw
    k = 25.4 / dpi / scale
    return cx / scale, cy / scale, rw * k, rh * k, ang


def detect_bbox_mm(png_path, dpi):
    """轴对齐兜底检测：行/列均值剖面 vs 远角背景。均值把噪声压低几十倍，
    所以合盖扫(白纸 vs 白压盖只差几个灰阶)也认得出，比逐像素阈值稳得多。
    返回 (x1,y1,x2,y2 像素, w_mm, h_mm)；失败返回 None。"""
    from PIL import Image
    import numpy as np
    a = np.asarray(Image.open(png_path).convert("L"), dtype=np.float32)
    h, w = a.shape
    bh, bw = max(1, int(h * .08)), max(1, int(w * .08))
    corner = a[h - bh:, w - bw:]
    bg = float(np.median(corner))
    # 均值剖面的标准误 = σ/√n；实测本机空台面 σ≈0，1.2 已是标准误的十几倍
    margin = max(1.2, float(np.std(corner)) * 6 / max(1.0, np.sqrt(min(h, w))))
    # 注：试过一次多项式去趋势(想抗灯管衰减)，拟合被大面积纸张带偏，反而全面变差，已回退
    cols = _largest_run(np.abs(a.mean(axis=0) - bg) > margin)
    rows = _largest_run(np.abs(a.mean(axis=1) - bg) > margin)
    if not cols or not rows:
        return None
    x1, x2 = cols
    y1, y2 = rows
    w_mm, h_mm = (x2 - x1) / dpi * 25.4, (y2 - y1) / dpi * 25.4
    if w_mm < 40 or h_mm < 40:
        return None
    return x1, y1, x2, y2, w_mm, h_mm


def _trim_device_padding(im, dpi):
    """裁掉扫描仪补的"合成白填充"（在内存里做，不落盘）。
    ADF 送入满宽但较短的纸(最常见 A4 横放)时，设备只按请求窗口高度返回，尾部用纯白填充——
    不裁就会被当成 A3 交付(A4 内容 + 下半页大白边)，这正是"扫描大小不正常"的 ADF 成因。
    判别(实测可完全分离)：填充行 mean>=254.5 且 std<=0.6（合成饱和白）；
    真实纸白 mean≈253.6 且 std 7~11（有纸纹噪声）。只在填充块 >10mm 时才裁，避免啃掉真内容。
    返回 (图, w_mm, h_mm, 底部是否被裁过)——底部裁过时量到的高度只是"至少这么高"，
    后面吸附尺寸只许补白、不许再往上裁(否则 A4 页脚会被当成 Letter 裁掉 6mm，F06)。"""
    import numpy as np
    a = np.asarray(im.convert("L"), dtype=np.float32)
    h, w = a.shape

    def last_real(profile_mean, profile_std, n):
        pad = (profile_mean >= 254.5) & (profile_std <= 0.6)
        i = n - 1
        while i > 0 and pad[i]:
            i -= 1
        return i + 1

    hh = last_real(a.mean(axis=1), a.std(axis=1), h)
    ww = last_real(a.mean(axis=0), a.std(axis=0), w)
    del a
    min_trim = 10 / 25.4 * dpi                      # 少于 10mm 的不折腾
    nh = hh if (h - hh) > min_trim else h
    nw = ww if (w - ww) > min_trim else w
    if (nw, nh) != (w, h):
        im = im.crop((0, 0, nw, nh))
    return im, nw / dpi * 25.4, nh / dpi * 25.4, nh < h


def _trim_adf_side_margins(im, dpi, max_mm=None):
    """裁掉 ADF 加宽窗口里**纸外**的左右两条（配合 ADF_EDGE_MARGIN，在内存里做）。
    纸外两种长相：①设备合成白(mean>=254.5 且 std<=0.6) ②深色背板(列均值 <160)。
    ⚠️ 两种判据不能混在一段里用(C03)：早期"合成白或深色"一起算，深色封面/深色页边会被当成背板，
    每边白白裁掉 ~4mm(连骑缝章)。现在每一侧先看窗口最边上那一列属于哪一类，只沿同一类往里走；
    深色这一类还必须在限度内碰到明显变亮的纸边(>200)才算背板，否则一刀不裁。
    返回 (图, w_mm, h_mm, 左裁mm, 右裁mm, 纸比窗口宽?)——左右各裁多少是后面吸附裁切的锚点
    (进纸偏了时纸外白边不对称，实测左 6mm/右 2mm，居中裁会啃掉纸的另一缘)。
    "纸比窗口宽"= 两侧都没有纸外区域、最外 2mm 都像纸：纸被截了(横放的纸/A3 进进纸器)，要告诉用户。"""
    import numpy as np
    a = np.asarray(im.convert("L"), dtype=np.float32)
    h, w = a.shape
    cm, cs = a.mean(axis=0), a.std(axis=0)
    del a
    pad = (cm >= 254.5) & (cs <= 0.6)
    dark = cm < 160
    lim = int(((ADF_EDGE_MARGIN + 4) if max_mm is None else max_mm) / 25.4 * dpi)
    near = max(1, int(1 / 25.4 * dpi))

    def side(order):
        if not len(order):
            return 0
        cls = pad if pad[order[0]] else dark if dark[order[0]] else None
        if cls is None:
            return 0
        cap = lim if cls is pad else lim + 2 * near     # 背板多看 2mm：进纸偏 4mm 时背板正好 10mm 宽
        n = 0
        while n < len(order) and n < cap and cls[order[n]]:
            n += 1
        if cls is dark:
            # 背板必须在限度内结束，且紧接着明显变亮(纸)：比背板亮 60 灰阶以上，或本身 >200。
            # 纸边那一列可能压着红章/字而偏暗，所以不能只认 >200。
            nxt = order[n:n + near]
            if n >= cap or not len(nxt) or not (cm[nxt].max() > 200 or cm[nxt].max() - cm[order[:n]].mean() > 60):
                return 0
        return n

    left = side(np.arange(0, w))
    right = side(np.arange(w - 1, -1, -1))
    if left + right >= w:
        left = right = 0
    min_trim = 1 / 25.4 * dpi                       # 少于 1mm 不折腾
    if left < min_trim:
        left = 0
    if right < min_trim:
        right = 0
    edge = max(1, int(2 / 25.4 * dpi))
    paperlike = ~pad & ~dark
    wider = (left == 0 and right == 0 and w > 2 * edge
             and bool(paperlike[:edge].all()) and bool(paperlike[-edge:].all()))
    if left or right:
        im = im.crop((left, 0, w - right, h))
    return (im, (w - right - left) / dpi * 25.4, h / dpi * 25.4,
            left / dpi * 25.4, right / dpi * 25.4, wider)


def widened_size(w_mm, h_mm, tol=6.0):
    """ADF 加宽窗口专用：高接近某标准尺寸、宽在 [标准-tol, 标准+2×边距+2] 之间 → 判为该尺寸
    （多出来的是纸外白边，由调用方按两侧剩余边距裁掉）。"""
    for name, sw, sh in STD_SIZES:
        for pw, ph in ((sw, sh), (sh, sw)):
            if abs(h_mm - ph) <= tol and -tol <= (w_mm - pw) <= 2 * ADF_EDGE_MARGIN + 2:
                return name, pw, ph
    return None


def normalize_pages(pages, dpi, truncated=None):
    """ADF(进纸器)专用：**每页各自**保持扫描仪返回的实际幅面，只做这些——
    ① 裁掉设备补的合成白和加宽窗口的纸外两条 ② 差一点点就吸附到标准尺寸(裁/补白边)
    ③ 强制写 DPI 元数据(缺了 img2pdf 会按 96dpi 算出 308x438mm 这种怪页面)。
    每页只读一次、存一次(早期每一步各存一次全分辨率 PNG，一页要编码 3~4 次)。
    绝不找纸边、绝不旋转：ADF 里纸铺满画面没有纸边可找，硬找会抓到正文文字块，
    进而把整页裁错甚至转歪(实测 p-000 被误判 -5.6° 真转歪了)。
    truncated：传一个 list 进来，纸比扫描窗口宽(两边被截、又吸附不到标准尺寸)的**页面文件路径**会追加进去
    (调用方在剔除空白页之后再换算成最终 PDF 里的页码)。"""
    from PIL import Image
    names = []
    for idx, p in enumerate(pages):
        try:
            im = Image.open(p)
            im.load()
            cut_l = cut_r = 0.0
            bottom_trimmed = wider = False
            try:
                im, w_mm, h_mm, bottom_trimmed = _trim_device_padding(im, dpi)
                im, w_mm, h_mm, cut_l, cut_r, wider = _trim_adf_side_margins(im, dpi)
            except Exception:
                w_mm = h_mm = None
            w, h = im.size
            if w_mm is None:
                w_mm, h_mm = w / dpi * 25.4, h / dpi * 25.4
            # ① 两边都很接近标准尺寸 → 直接吸附；② 差一点点(≤14mm)能装下 → 补白边还原成标准纸；
            # ③ 都不满足(如 260×209 的收据) → 如实保留实测尺寸，不贴假标签。
            # 底部被裁过(量到的高度只是下限)时，比量到的还矮的候选一律不要——只许补白，不许再裁掉页脚。
            cands = (nearest_size(w_mm, h_mm, tol=6.0), widened_size(w_mm, h_mm),
                     snap_up_size(w_mm, h_mm, slack=14.0))
            snapped = next((c for c in cands if c and (not bottom_trimmed or c[2] >= h_mm - 0.5)), None)
            # 只有"两边都贴满窗口 而且 吸附不到任何标准尺寸"才算纸被截——纸外若是带噪声的白(真机长相待验)，
            # 正常 A4 也会两边贴满，但它能吸附回 A4，不能每页都报警
            if wider and not snapped and truncated is not None:
                truncated.append(p)
            if snapped:
                name, pw, ph = snapped
                tw, th = round(pw / 25.4 * dpi), round(ph / 25.4 * dpi)
                if (tw, th) != (w, h):
                    # 宽度多出来的部分按**哪边纸外白边还剩得多**分配：每侧本来各多要了
                    # ADF_EDGE_MARGIN，已裁掉 cut_l/cut_r，剩余 = 边距-已裁。2026-08-26 实测进纸
                    # 偏了(左裁 6/右裁 2)，居中各裁一半会啃掉纸右缘 2mm(骑缝章所在)；左锚更糟
                    # (2026-08-25 事故)。辨不出来(两侧都没裁到)才退回居中。
                    rl = max(0.0, ADF_EDGE_MARGIN - cut_l)
                    rr = max(0.0, ADF_EDGE_MARGIN - cut_r)
                    frac = rl / (rl + rr) if (rl + rr) > 0 else 0.5
                    x0 = max(0, min(w - tw, int(round((w - tw) * frac)))) if w > tw else 0
                    box = im.crop((x0, 0, x0 + min(tw, w), min(th, h)))
                    if box.size != (tw, th):            # 小了补白边，不拉伸不丢内容
                        canvas = Image.new(im.mode, (tw, th), "white")   # ⚠️ 整数 255 在 RGB 里是纯红(255,0,0)
                        canvas.paste(box, (0, 0))
                        box = canvas
                    im = box
                names.append(name)
            else:
                names.append("%.0f×%.0fmm" % (w_mm, h_mm))
            im.save(p, dpi=(dpi, dpi))
        except Exception:
            names.append("Original")
    uniq = sorted(set(names))
    return "/".join(uniq) if len(uniq) <= 3 else "%s and %d different sizes" % (uniq[0], len(uniq))


# ---- Feeder (smfp) geometry, measured on the real device 2026-10-19 ----
# The old code opened a window centred where the paper "should" be and cropped it back to 210 mm: a sheet 1 mm off
# centre lost 1 mm of its edge (where cross-page seals sit), A4 fed sideways lost 79 mm, anything longer than A4 was
# cut at 297 mm - and every one of those jobs reported "done". Raw frames (-l 0 -x 297 -y 432) show:
#   * ~2 mm of pure white (255) at both window sides and at the paper's leading edge: drawn by the scanner firmware;
#     everything after the paper's end is white filler added by HP's driver.
#   * beside the paper the feeder backing reads 252-253 (only ~1 level from paper) but does NOT change along the feed:
#     mean row-to-row difference per 25 mm band is 0.005-0.09 for the backing and 0.095-0.9 for paper (moving fibres).
# adf_find_paper / adf_finish_page find the real paper edge from that texture. They are NOT used by scanning any more
# (scans are plain scanner output since the owner's 2026-10-19 decision); they remain as the engine for an explicit
# "crop to the paper edge" action. normalize_pages likewise only serves its old tests.
ADF_WIN_W, ADF_WIN_H = 297, 432       # with -l 0 the feeder takes -x 0..297 and -y 0..432 mm
# Feeder window for a chosen paper size. Measured 2026-10-19 by scanning the same sheets through HP's A4 window and
# through the full-width window:
#   * this feeder carries the paper at 44.6-255.1 mm across (centre ~150 mm); HP's driver centres its A4 window on
#     148 mm, at 43-253 mm;
#   * the scanner firmware whites out ~1.4-2 mm at both window sides and ~2 mm at the leading edge (no driver setting
#     controls this);
#   -> HP's unchanged A4 window loses 3.6-3.8 mm of the paper's right edge on every page (where cross-page seals sit).
#   So the window is centred on 150 mm with 5 mm extra per side (2 mm firmware strip + 3 mm feed drift) and 3 mm extra
#   length: A4 -> -l 40 -x 220 -y 300. No image processing: pages come out slightly larger than A4 (220x300 mm) with
#   the whole paper edge on them.
ADF_PAPER_CENTER_MM = 150.0
ADF_SIDE_ROOM_MM = 5.0
ADF_END_ROOM_MM = 3.0


def adf_window(w, h):
    """Feeder window [-l, -t, -x, -y] in whole millimetres for a w x h mm sheet (see the measurements above)."""
    ww = min(ADF_WIN_W, w + 2 * ADF_SIDE_ROOM_MM)
    left = min(max(0.0, ADF_PAPER_CENTER_MM - ww / 2), ADF_WIN_W - ww)
    return [int(round(left)), 0, int(round(ww)), int(round(min(ADF_WIN_H, h + ADF_END_ROOM_MM)))]


def edge_check(path, dpi, whole_path=False):
    """Read-only check (the image is never changed): could content be hidden at an edge the scanner cannot see?
    Returns (issues, notes). The firmware whites out ~2 mm at both window sides and at the paper's leading edge.
    * One side strip: the sheet running into it (paper texture right up to the strip) means it was fed off centre
      - a cross-page seal can vanish completely under the strip - or ink touches it: needs a check.
    * Both side strips: in the whole feed path the sheet is as wide as the path (sideways A4, A3) and its outer few
      mm are inherently out of reach: plain note, unless there is ink near either edge. In a chosen-size window the
      sheet is wider than the chosen size (typically A4 fed sideways): needs a check.
    * Leading edge: every sheet runs under that strip, so only ink touching it counts.
    * No driver filler at either end: the sheet filled the window's length and is probably longer (needs a check).
    Paper is told from the backing by its row-to-row texture (floor scaled by 300/dpi); the window centre, always
    on the sheet, only decides whether the sheet has enough texture to be judged.
    whole_path: the window is the entire 297 mm feed path ("Full area"); only then is a sheet that touches both side
    strips an inherent case (a note). In a chosen-size window it means the sheet is wider than the chosen size.
    Memory: only row minima and a few edge bands are materialised, never full-size float copies."""
    from PIL import Image
    import numpy as np
    with Image.open(path) as im:
        im = im.convert("RGB") if im.mode != "RGB" else im
        im.load()
        W, H = im.size
        k = dpi / 25.4

        def region(x0, y0_, x1, y1_):
            return np.asarray(im.crop((max(0, x0), max(0, y0_), min(W, x1), min(H, y1_))))

        rowpure = np.zeros(H, bool)
        for y in range(0, H, 256):
            rowpure[y:y + 256] = (region(0, y, W, y + 256).min(axis=2) >= 254).all(axis=1)
        rows = np.flatnonzero(~rowpure)
        if not len(rows):
            return [], []
        y0, y1 = int(rows[0]), int(rows[-1]) + 1
        edge = min(W // 2, int(round(8 * k)))       # the strips (<= ~2.5 mm) and the bands next to them

        def near_white_fraction(x0, x1):
            return (region(x0, y0, x1, y1).min(axis=2) >= 254).mean(axis=0)

        def strip_width(frac):
            # ~1.4 mm pure white plus ~0.5 mm fading unevenly into the image (real frames: columns 80-100 %
            # near-white, one dipping to 42 %). The strip ends where three columns in a row are mostly NOT white;
            # otherwise the paper test would look at the fade and miss paper running under the strip. The search
            # stops at 2.5 mm: on paper the scanner drives to pure white (or a grey backing reading 254) it would
            # run on into the page, and the checks would look millimetres away from the strip.
            run = 0
            for n, f in enumerate(frac[:max(3, int(round(EDGE_STRIP_MAX_MM * k)))]):
                run = run + 1 if f < 0.5 else 0
                if run >= 3:
                    return n - 2
            return min(len(frac), int(round(EDGE_STRIP_MM * k)))

        pl = strip_width(near_white_fraction(0, edge))
        pr = strip_width(near_white_fraction(W - edge, W)[::-1])
        touch = max(3, int(round(0.6 * k)))           # "touching": within 0.6 mm of a strip
        near = max(touch, int(round(5 * k)))           # "near": within 5 mm of a strip
        span = max(3, int(round(0.7 * k)))             # paper test on the 0.7 mm next to each strip
        m1 = max(1, int(round(k)))
        need = 8
        c = max(span, min(W // 2 - edge, int(round(10 * k))))
        # ink = clearly darker or redder than this sheet's own paper: pink carbonless forms read ~194 in their
        # darkest channel and ~30 redder than neutral, so fixed limits counted the sheet itself as ink or seal
        mid = region(W // 2 - c, y0, W // 2 + c, y1).astype(np.int16)
        ink_lvl, red_lvl = 200, 40
        if mid.size:
            ink_lvl = min(200, int(np.median(mid.min(axis=2))) - 50)
            red_lvl = 40 + max(0, int(np.median(mid[..., 0] - np.maximum(mid[..., 1], mid[..., 2]))))

        def ink_count(box):
            r = region(*box).astype(np.int16)
            if not r.size:
                return 0
            dark = r.min(axis=2) < ink_lvl
            red = (r[..., 0] - np.maximum(r[..., 1], r[..., 2])) >= red_lvl
            return int((dark | red).sum())

        def texture(x0, x1):
            # median over columns of the mean row-to-row change, ink excluded (pixels darker than this band's own
            # level - 25, so white and coloured paper alike): the moving sheet changes from row to row, the static
            # backing beside it barely does
            if x1 <= x0 or y1 - y0 <= 4 * m1:
                return None
            g = region(x0, y0 + m1, x1, y1 - m1)[..., 1].astype(np.int16)
            if not g.size:
                return None
            lvl = float(np.median(g)) - 25
            ok = (g[1:] >= lvl) & (g[:-1] >= lvl)
            n = ok.sum(axis=0)
            d = (np.abs(np.diff(g, axis=0)) * ok).sum(axis=0)
            cols = n >= max(8, (g.shape[0] - 1) // 4)
            return float(np.median(d[cols] / n[cols])) if cols.any() else None

        # Paper beside a strip = texture above a floor that grows with the line pitch like the backing's flicker
        # does (a fixed 300 dpi threshold flagged the backing at 150/100 dpi). The window centre - always on the
        # sheet - only says whether this sheet has enough texture to be seen at all; it never raises the threshold
        # (shading or dense text in the centre once hid a sheet under a strip). Very white paper, grey at low dpi:
        # only ink is checked, and the page says so instead of passing as complete.
        floor = ADF_EDGE_TEX * 300.0 / max(dpi, 1)
        ref = texture(W // 2 - c, W // 2 + c)
        sure = ref is not None and ref >= EDGE_SURE * floor

        def paper_at(x0, x1):
            if not sure or x1 <= x0 or y1 - y0 <= 4 * m1:
                return False
            t = texture(x0, x1)
            return t is None or t > floor             # all ink right at a strip: treat as paper

        paper_l = pl < W and paper_at(pl, pl + span)
        paper_r = pr < W and paper_at(W - pr - span, W - pr)
        lost, notes, wide = [], [], False
        if paper_l and paper_r:
            if not whole_path:
                wide = True                              # wider than the chosen size, typically A4 fed sideways
            elif (ink_count((pl, y0, pl + near, y1)) >= need
                    or ink_count((W - pr - near, y0, W - pr, y1)) >= need):
                lost += ["left", "right"]
            else:
                notes.append(EDGE_NOTE_FULL_WIDTH)
        else:
            if pl < W and (paper_l or ink_count((pl, y0, pl + touch, y1)) >= need):
                lost.append("left")
            if pr < W and (paper_r or ink_count((W - pr - touch, y0, W - pr, y1)) >= need):
                lost.append("right")
        if not sure:
            notes.append("The paper edge is indistinct at this color or resolution. Edge text and seals were checked, but the physical edge could not be verified")
        if ink_count((0, y0, W, y0 + touch)) >= need:
            lost.append("leading")
        # A sheet that ends inside the window leaves driver filler (>= 2.9 mm in every chosen-size window) at one
        # end; one that fills the window shows only the firmware bands at both ends (measured: 1.35 mm leading,
        # 1.6-1.95 mm at the end of HP's 297 mm A4 window on an A4 sheet, whose last ~2 mm went unseen). Either end,
        # so a back side delivered upside down reads the same.
        too_long = y0 / k < EDGE_END_FILL_MM and (H - y1) / k < EDGE_END_FILL_MM
    issues = []
    if wide:
        issues.append("The sheet is wider than the selected size, often because it was fed sideways; both side edges are incomplete")
    if lost:
        issues.append("The %s edge contains content or enters an unseen scanner strip. A narrow strip, such as part of a seal, may be missing" % "、".join(lost))
    if too_long:
        issues.append("The sheet is longer than the scan area; its trailing edge may be missing")
    return issues, notes


EDGE_SURE = 1.3                       # edge_check: paper beside a strip = texture > ADF_EDGE_TEX x 300/dpi (backing
                                      # measured <= 0.63 x that at 300-100 dpi; paper at a strip 1.5-7 x); the test
                                      # runs only if the sheet's own texture is >= 1.3 x it (real paper 300 dpi: white
                                      # 0.34-0.78, pink forms ~1.8; 150 dpi: 0.25-1.1; very white paper ~0: not run)
EDGE_END_FILL_MM = 2.4                # edge_check: white at both ends below this = the sheet filled the window length
                                      # (real: 1.35 mm leading band, 1.6-1.95 mm end band; a sheet that fits leaves
                                      # 2.9 mm (Letter) to 3.3 mm (A4))
EDGE_NOTE_FULL_WIDTH = "The sheet fills the feeder width (sideways A4 or A3). A few millimeters on each long edge are unseen; feed A4 short edge first to include both sides"
EDGE_ADVICE = ("Next: Align the guides and retry. Rotate the stack if a seal is on the leading edge. For wider or sideways sheets, choose Full area or feed A4 short edge first")
EDGE_STRIP_MM = 1.95                  # edge_check: measured side strip incl. its fade (23 px at 300 dpi)
EDGE_STRIP_MAX_MM = 2.5               # edge_check: the strip search never goes further in than this

# Paper white (owner decision 2026-10-19 18:40 SGT: on by default, coloured paper kept, untouched original kept).
# HP's driver always asks the printer for background removal OFF and colour management OFF, so white paper arrives
# as (255, ~251, 255): faint pink and not white. Method E of docs/scanner-paper-white-research-2026-10-19.md, measured
# on real M437 frames: 99.4-99.9 % of paper pixels become exactly 255, seals and signatures move < 1 deltaE00.
PAPER_WHITE_KNEE = 8.0                # levels below the white point that roll smoothly up to 255
PAPER_WHITE_MARGIN = 2.5              # minimum noise margin (levels) between the paper colour and the white point
PAPER_WHITE_MAX_GAIN = 1.45           # never boost a channel more than this
PAPER_WHITE_KEEP_C = 10.0             # paper with CIELAB chroma above this is coloured paper: left as scanned
                                      # (white paper measured 2.5, pink carbonless forms ~25)
PAPER_WHITE_MIN = 150                 # paper darker than this in any channel is no paper (dark cover, photo)


def _paper_colour(a, feeder):
    """Robust paper colour of a uint8 RGB (H, W, 3) or L (H, W) array: (median, sigma) per channel, or None.
    Pure white is the driver's filler after the page end and is ignored. On feeder pages only a 50 mm band at the
    window centre is used: the sheet is always centred there, while the backing beside a narrow slip is brighter
    than paper and was taken for it. The paper is the brightest luminance peak holding >= 15 % of the bright
    pixels, so a large light-tinted box is not mistaken for the paper."""
    import numpy as np
    if a.ndim == 2:
        a = a[:, :, None]
    H, W = a.shape[:2]
    if feeder:
        c = min(W // 4, max(1, W * 25 // 297))           # +-25 mm of a 297 mm wide frame, at most half the width
        a = a[:, max(0, W // 2 - c):W // 2 + c]
    s = a[::4, ::4].reshape(-1, a.shape[2])
    t = s[~(s == 255).all(axis=1)]
    if len(t) < 0.2 * len(s):
        return None                                   # nearly all filler: nothing to measure
    lum = t.astype(np.float32) @ (np.array([0.299, 0.587, 0.114], np.float32) if t.shape[1] == 3
                                  else np.ones(1, np.float32))
    bright = lum > 128
    if bright.sum() < 0.1 * len(t):
        return None                                   # dark page
    hist = np.bincount(np.round(lum[bright]).astype(np.int64), minlength=257)[:257].astype(np.float64)
    smooth = np.convolve(hist, np.ones(5) / 5.0, mode="same")
    cum = np.concatenate([[0.0], np.cumsum(hist)])
    total = float(hist.sum())
    peaks = [v for v in range(1, 256) if smooth[v] > 0 and smooth[v] >= smooth[v - 1] and smooth[v] >= smooth[v + 1]
             and cum[min(257, v + 7)] - cum[max(0, v - 6)] >= 0.15 * total]
    peak = max(peaks) if peaks else int(np.argmax(smooth))
    cluster = t[bright][np.abs(lum[bright] - peak) <= 6].astype(np.float32)
    if not len(cluster):
        return None
    med = np.median(cluster, axis=0).astype(np.float64)
    sig = np.maximum(np.median(np.abs(cluster - med), axis=0) * 1.4826, 0.5).astype(np.float64)
    return med, sig


def _lab_chroma(rgb):
    """CIELAB chroma of one sRGB colour (D65)."""
    import numpy as np
    c = np.asarray(rgb, np.float64) / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]]) @ c
    xyz = xyz / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 216 / 24389, np.cbrt(xyz), (24389 / 27 * xyz + 16) / 116)
    return float(np.hypot(500 * (f[0] - f[1]), 200 * (f[1] - f[2])))


def whiten_paper(path, dpi, feeder=True):
    """Make the paper pure white, in place: one lookup table per channel scales the paper colour to white with a
    common noise margin and a soft knee; black stays black, so ink, seals and signatures barely move. Coloured paper,
    dark pages and pages without measurable paper are left as scanned. The caller keeps the untouched frame.
    Returns what was done, for record.json: {"paper", "gain"} or {"kept": reason}."""
    import numpy as np
    from PIL import Image
    with Image.open(path) as im:
        if im.mode not in ("RGB", "L"):
            return {"kept": "mode %s" % im.mode}
        im.load()
        est = _paper_colour(np.asarray(im), feeder)
        if est is None:
            return {"kept": "no paper to measure"}
        med, sig = est
        if med.min() < PAPER_WHITE_MIN:
            return {"kept": "dark page", "paper": med.round(1).tolist()}
        if im.mode == "RGB":
            chroma = _lab_chroma(med)
            if chroma > PAPER_WHITE_KEEP_C:
                return {"kept": "coloured paper", "paper": med.round(1).tolist(), "chroma": round(chroma, 1)}
        # noise margin below the paper colour; at least 2.5 levels: the centre band's noise can read near 0 on
        # integer data while the paper varies more across the page (a real frame reached only 98.8 % with 1 level)
        margin = max(PAPER_WHITE_MARGIN, 2.0 * float(np.max(sig)))
        gain = np.minimum(255.0 / np.maximum(med, 1.0) * 255.0 / (255.0 - margin), PAPER_WHITE_MAX_GAIN)
        v = np.arange(256, dtype=np.float64)
        luts = []
        for g in gain:
            out = v * g
            w0 = 255.0 / g
            t = np.clip((v - (w0 - PAPER_WHITE_KNEE)) / PAPER_WHITE_KNEE, 0.0, 1.0)
            out = out + (255.0 - out) * (t * t * (3 - 2 * t))
            luts.append(np.clip(np.round(out), 0, 255).astype(np.uint8))
        out_im = im.point(np.concatenate(luts).tolist())
    out_im.save(path, dpi=(dpi, dpi))
    return {"paper": med.round(1).tolist(), "gain": [round(float(g), 4) for g in gain]}
ADF_PAPER_TEX = 0.05                  # a column can only be backing if its median band texture is below this
                                      # (backing ~0.01, paper ~0.2)
ADF_EDGE_TEX = 0.10                   # growing the edge outward: paper while any band is above this (backing peaks
                                      # at ~0.09; on a skewed sheet the edge columns are paper in some bands only)
ADF_BACK_MEAN = (250.0, 254.0)        # column-mean range of the backing (measured 252-253.3); brighter (paper driven
                                      # to pure white) or darker (dark paper) counts as paper
ADF_SMOOTH_MM = 0.76                  # texture smoothing width (9 columns at 300 dpi), fixed in mm so the edge bias
                                      # does not change with resolution
ADF_EDGE_BIAS_MM = 0.35               # smoothing pushes each measured edge ~0.35 mm outward (real A4 measures 210.7);
                                      # only used to recognise the size
ADF_MIN_CONTRAST = (0.12, 4.0)        # paper median texture must be >= max(0.12, 4 x backing). Only measured at
                                      # 300 dpi colour (284 of 285 feeder jobs); with less contrast: never crop
ADF_KEEP_MM = 1.0                     # margin kept outside the found paper edge (outside only, never into the paper)
ADF_SNAP_CUT_MM = 1.0                 # most (bias-corrected) paper width a standard-size snap may cut, both sides
                                      # together: the edge transition, <= 0.5 mm per side
ADF_DESKEW_MIN_DEG = 0.15             # straighten only when both paper edges agree on at least this much skew
                                      # (measured 0.01-0.1 deg)
ADF_DESKEW_MAX_DEG = 3.0              # beyond this the measurement is probably wrong: do not rotate


def adf_find_paper(im, dpi):
    """Find the paper in a raw feeder frame. Returns a dict or None: x0, x1, y0, y1 (pixels), pad_l/pad_r (width of
    the device's white side strips),
    touches_l/r/end (the paper runs into the device's white strip / the end of the window: that side of the sheet is
    larger than the scan area and content near it may be missing), skew_deg (from dx/dy of both paper edges:
    positive = the paper sits rotated counter-clockwise; straighten with rotate(-skew); None when not measurable).
    The BACKING is recognised first and everything else counts as paper: backing = no change along the feed + grey
    level in the backing range + not the device's pure white. The reverse ("only textured columns are paper") breaks
    on very white paper the device drives to pure 255: it has no texture, only the text block would count as paper,
    and the crop would cut into the sheet."""
    import numpy as np
    a = np.asarray(im.convert("L"))
    H, W = a.shape
    k = dpi / 25.4
    m1 = max(1, int(round(k)))
    rows = np.flatnonzero((a < 254).sum(axis=1) > 0.005 * W)      # rows with paper (backing/paper < 254, device white = 255)
    if len(rows) < int(40 * k):
        return None
    y0, y1 = int(rows[0]), int(rows[-1]) + 1
    lo, hi = y0 + m1, y1 - m1
    band = max(32, int(round(25 * k)))
    kn = max(3, int(round(ADF_SMOOTH_MM * k)) | 1)
    tex, mids = [], []
    for s in range(lo, hi - 16, band):
        blk = a[s:min(s + band, hi)].astype(np.int16)
        if len(blk) < 16:
            continue
        t = np.abs(np.diff(blk, axis=0)).mean(axis=0)
        tex.append(np.convolve(t, np.ones(kn) / kn, mode="same"))
        mids.append(s + len(blk) / 2.0)
    if not tex:
        return None
    tex = np.array(tex)
    med, top = np.median(tex, axis=0), tex.max(axis=0)
    body = a[lo:hi]
    colmin = body.min(axis=0)
    colmean = body.mean(axis=0)
    del body
    pad_l = 0
    while pad_l < W and colmin[pad_l] >= 254:
        pad_l += 1
    pad_r = 0
    while pad_r < W - pad_l and colmin[W - 1 - pad_r] >= 254:
        pad_r += 1
    lim_l, lim_r = pad_l, W - pad_r
    if lim_r - lim_l < 40 * k:
        return None
    in_range = (colmean >= ADF_BACK_MEAN[0]) & (colmean <= ADF_BACK_MEAN[1]) & (colmin < 254)
    backing = (med < ADF_PAPER_TEX) & in_range              # core test: static in most bands
    still = (top <= ADF_EDGE_TEX) & in_range                # edge test: static in every band
    paper = ~backing
    paper[:lim_l] = False
    paper[lim_r:] = False
    if not paper.any():
        return None
    # The sheet = the paper run through the window centre (the guides centre it); else the longest paper run
    c = W // 2
    if not paper[c]:
        edges = np.flatnonzero(np.diff(np.concatenate(([0], paper.astype(np.int8), [0]))))
        c0, c1 = max(zip(edges[0::2], edges[1::2]), key=lambda r: r[1] - r[0])
        c = (c0 + c1) // 2
    x0 = c
    while x0 > lim_l and paper[x0 - 1]:
        x0 -= 1
    x1 = c
    while x1 < lim_r and paper[x1]:
        x1 += 1
    while x0 > lim_l and not still[x0 - 1]:                 # skewed sheet: edge columns are paper in some bands only
        x0 -= 1
    while x1 < lim_r and not still[x1]:
        x1 += 1
    if x1 - x0 < 40 * k:
        return None
    # Not enough texture contrast between paper and backing: give up - the caller keeps the whole frame rather than
    # cutting along an unreliable edge
    back_cols = backing.copy()
    back_cols[:lim_l] = False
    back_cols[lim_r:] = False
    if back_cols.any():
        pt = float(np.median(med[x0:x1]))
        bt = float(np.median(med[back_cols]))
        if pt < max(ADF_MIN_CONTRAST[0], ADF_MIN_CONTRAST[1] * bt):
            return None
    near = max(2, int(round(0.5 * k)))
    touches_l, touches_r = x0 - lim_l <= near, lim_r - x1 <= near
    # Skew: left/right paper edge per band -> linear fit. Only counts when both edges are measured and agree
    skew = None
    if not (touches_l or touches_r) and len(mids) >= 4:
        le, re_ = [], []
        for t in tex:
            p = (t >= ADF_EDGE_TEX) | ~in_range
            p[:x0] = False
            p[x1:] = False
            idx = np.flatnonzero(p)
            le.append(idx[0] if len(idx) else np.nan)
            re_.append(idx[-1] + 1 if len(idx) else np.nan)
        ys, le, re_ = np.array(mids), np.array(le, float), np.array(re_, float)
        ok = ~np.isnan(le) & ~np.isnan(re_)
        if ok.sum() >= 4:
            sl = np.degrees(np.arctan(np.polyfit(ys[ok], le[ok], 1)[0]))
            sr = np.degrees(np.arctan(np.polyfit(ys[ok], re_[ok], 1)[0]))
            if abs(sl - sr) <= 0.08:
                skew = float((sl + sr) / 2)
    return {"x0": int(x0), "x1": int(x1), "y0": y0, "y1": y1, "W": W, "H": H,
            "pad_l": pad_l, "pad_r": pad_r, "touches_l": bool(touches_l), "touches_r": bool(touches_r),
            "touches_end": H - y1 <= m1, "skew_deg": skew}


def _ink_near(a, axis, lo, hi, strip):
    """Is there ink (dark or red) in the strip lo..hi of the RGB uint8 array a (columns if axis=1, rows if 0)?"""
    import numpy as np
    blk = a[:, lo:hi] if axis == 1 else a[lo:hi, :]
    if blk.size == 0:
        return False
    g = blk.min(axis=2).astype(np.int16)
    red = (blk[..., 0].astype(np.int16) - np.maximum(blk[..., 1], blk[..., 2]).astype(np.int16) >= 40)
    return int(((g < 200) | red).sum()) >= max(8, strip // 2)


def adf_finish_page(path, dpi):
    """One feeder page: find the paper -> crop only outside it -> if the sheet is a standard size, pad it to that exact
    size, else keep the measured size. Returns (size label, issues needing a check, plain notes, geometry record).
    Overwrites the file in place and keeps the DPI. Never cuts more than ADF_SNAP_CUT_MM/2 into the paper (the edge
    transition); asks for a check only when the sheet exceeds the scan area AND there is ink at that edge.
    Not used by scanning since 2026-10-19 (plain output); kept for an explicit "crop to the paper edge" action."""
    from PIL import Image
    import numpy as np
    im = Image.open(path)
    im.load()
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    k = dpi / 25.4
    issues, notes = [], []
    info = adf_find_paper(im, dpi)
    W, H = im.size
    if info is None:
        a = np.asarray(im.convert("L"))
        rows = np.flatnonzero((a < 254).sum(axis=1) > 0.005 * W)
        y1 = int(rows[-1]) + 1 if len(rows) else H
        del a
        if y1 < H:
            im = im.crop((0, 0, W, min(H, y1 + int(round(ADF_KEEP_MM * k)))))
        im.save(path, dpi=(dpi, dpi))
        notes.append("Paper edges were not detected; kept the full feeder area")
        return ("%.0f×%.0fmm" % (im.width / k, im.height / k), issues, notes,
                {"raw_px": [W, H], "paper_box_px": None, "out_px": list(im.size)})
    x0, x1, y0, y1 = info["x0"], info["x1"], info["y0"], info["y1"]
    rgb = np.asarray(im if im.mode == "RGB" else im.convert("RGB"))
    strip = max(4, int(round(3 * k)))
    lost = []
    if info["touches_l"] and _ink_near(rgb[y0:y1], 1, x0, x0 + strip, strip):
        lost.append("left")
    if info["touches_r"] and _ink_near(rgb[y0:y1], 1, x1 - strip, x1, strip):
        lost.append("right")
    if _ink_near(rgb[:, x0:x1], 0, y0, y0 + strip, strip):
        lost.append("leading")                       # firmware whites out ~2 mm of the leading edge: ink there is clipped
    if info["touches_end"]:
        lost.append("trailing")
    del rgb
    if lost:
        issues.append("Content reaches the %s edge of the scan area. A narrow strip may be missing. Check the result; rotate the stack or feed short edge first if needed" % "、".join(lost))
    both = info["touches_l"] and info["touches_r"]
    skew = info.get("skew_deg")
    if skew is not None and ADF_DESKEW_MIN_DEG <= abs(skew) <= ADF_DESKEW_MAX_DEG:
        # Straighten by the angle of the two paper EDGES (not of text lines: text-based deskew turned upright pages
        # crooked and was dropped). Rotate about the paper centre; the sheet's own size follows from its bounding
        # box: box width = w*cos(t) + h*sin(t).
        th_ = np.radians(abs(skew))
        bw, bh = x1 - x0, y1 - y0
        den = np.cos(th_) ** 2 - np.sin(th_) ** 2
        pw_px = (bw * np.cos(th_) - bh * np.sin(th_)) / den
        ph_px = (bh * np.cos(th_) - bw * np.sin(th_)) / den
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        im = im.rotate(-skew, resample=Image.BICUBIC, center=(cx, cy), fillcolor="white")
        x0, x1 = int(round(cx - pw_px / 2)), int(round(cx + pw_px / 2))
        y0, y1 = int(round(cy - ph_px / 2)), int(round(cy + ph_px / 2))
        notes.append("The sheet was skewed %.1f degrees and has been aligned to its edges" % abs(skew))
    wp, hp = (x1 - x0) / k, (y1 - y0) / k
    snapped = None
    if not info["touches_end"]:
        wq = wp if both else wp - 2 * ADF_EDGE_BIAS_MM     # only for recognising the size: remove the smoothing bias
        best = None
        for name, sw, sh in STD_SIZES:
            for pw, ph in ((sw, sh), (sh, sw)):
                okw = (wp - 1.0 <= pw <= wp + 6.0) if both else (pw - 2.0 <= wq <= pw + ADF_SNAP_CUT_MM)
                okh = -1.0 <= ph - hp <= 6.0                   # the device loses ~1-2 mm at the leading/trailing edge
                if okw and okh:
                    d = abs(pw - wq) + abs(ph - hp)
                    if best is None or d < best[0]:
                        best = (d, name, pw, ph)
        snapped = best[1:] if best else None
    if both:
        notes.append("The sheet is wider than the scan area (sideways or A3); about 2 mm on each long edge is unseen")
    keep = int(round(ADF_KEEP_MM * k))
    if snapped:                                         # centre the paper on an exact standard page, pad with white
        name, pw, ph = snapped
        tw, th = int(round(pw * k)), int(round(ph * k))
        left = int(round((x0 + x1) / 2 - tw / 2))
        top = int(round((y0 + y1) / 2 - th / 2))
        label = name
    else:                                               # non-standard sheet: paper + 1 mm per side, labelled in mm
        left, top = max(0, x0 - keep), max(0, y0 - keep)
        tw = min(W, x1 + keep) - left
        th = min(H, y1 + keep) - top
        label = "%.0f×%.0fmm" % (tw / k, th / k)
    box = im.crop((max(0, left), max(0, top), min(W, left + tw), min(H, top + th)))
    if box.size != (tw, th):
        canvas = Image.new(box.mode, (tw, th), "white")     # not the integer 255: in RGB that is pure red
        canvas.paste(box, (max(0, -left), max(0, -top)))
        box = canvas
    box.save(path, dpi=(dpi, dpi))
    geo = {"raw_px": [W, H], "paper_box_px": [info["x0"], info["y0"], info["x1"], info["y1"]],
           "pads_px": [info["pad_l"], info["pad_r"]], "skew_deg": skew, "snapped": snapped[0] if snapped else None,
           "crop_origin_px": [left, top], "out_px": [tw, th],
           "touches": {k_: info[k_] for k_ in ("touches_l", "touches_r", "touches_end")}}
    return label, issues, notes, geo


def analyze_page(png_path, dpi):
    """只识别不裁切。返回 {name, pw, ph, ang, cx, cy, ax, ay} 或 None。
    拆成"识别"和"裁切"两步，是为了让多页批次能先投票出统一尺寸再一起裁——
    否则每页各认各的，会出现"几页正常几页大小不对"的混乱 PDF。"""
    # ① OpenCV 旋转矩形：认得出放歪的纸
    try:
        rot = detect_rotated(png_path, dpi)
    except Exception:
        rot = None
    # 只在"纸确实放歪了"时采用 CV 结果——那是它独有的价值；
    # 纸摆正时均值剖面法更稳(实测 CV 在低对比/噪声/满幅场景会把正文块当成纸)
    from PIL import Image
    IW, IH = Image.open(png_path).size
    img_area_mm = (IW / dpi * 25.4) * (IH / dpi * 25.4)

    def looks_like_paper(w_mm, h_mm):
        """纸张必须"够大够方"。实测不加这道闸会把正文文字块当成纸
        (踩过的真实误检：15×208mm、55×165mm 的细条)，进而裁错、甚至按文字块角度把整页转歪。
        两条细条的宽都 <60mm，所以"两边都≥60mm"这一条就足以挡住它们；
        面积闸放宽到 6%——原来 20% 会让 A6/发票/名片(A3 台面上仅占 12.5%)永远识别不出来，
        被迫交付整张 A3。"""
        return (w_mm >= 60 and h_mm >= 60 and w_mm * h_mm >= img_area_mm * 0.06)

    if rot and abs(rot[4]) > 1.0:
        cx, cy, w_mm, h_mm, ang = rot
        snapped = snap_size(w_mm, h_mm, tol=4.0) if looks_like_paper(w_mm, h_mm) else None
        if snapped:
            name, pw, ph = snapped
            return {"name": name, "pw": pw, "ph": ph, "ang": ang,
                    "cx": cx, "cy": cy, "ax": None, "ay": None}
    # ② 轴对齐兜底：低对比时更稳
    det = detect_bbox_mm(png_path, dpi)
    if not det:
        return None
    x1, y1, x2, y2, w_mm, h_mm = det
    # 闸门只认**原始检测框**：早期还接受"向原点延伸后"的尺寸，结果 55×165mm 的窄文字块
    # 延伸后变成 72×182mm 蒙混过关，被当成 A5 裁掉。延伸只用于定位，不能用来"洗白"尺寸。
    if not looks_like_paper(w_mm, h_mm):
        return None                                     # 只抓到文字块 → 认输，保持原样(绝不乱裁乱转)
    snapped = snap_size(w_mm, h_mm, tol=4.0)
    if snapped:
        ax, ay = x1, y1                                 # 贴合标准尺寸：按检测框位置裁
    else:                                               # 只认出正文区 → 向原点延伸(纸总是靠原点角放)
        x1 = 0 if x1 * 25.4 / dpi < 15 else x1
        y1 = 0 if y1 * 25.4 / dpi < 15 else y1
        snapped = snap_size((x2 - x1) / dpi * 25.4, (y2 - y1) / dpi * 25.4, tol=6.0)
        if not snapped:
            return None
        ax, ay = x1, y1
    name, pw, ph = snapped
    return {"name": name, "pw": pw, "ph": ph, "ang": 0.0,
            "cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2, "ax": ax, "ay": ay}


def fit_page(png_path, dpi, pw, ph, info):
    """把一页归一到精确的 pw x ph (mm)：需要就先摆正，大了裁、小了补白边(绝不丢内容)，
    并强制写入 DPI 元数据 —— 缺元数据时 img2pdf 会按 96dpi 换算出 308x438mm 这种怪页面。"""
    from PIL import Image
    im = Image.open(png_path)
    rotated = False
    if info and abs(info.get("ang", 0.0)) > 1.0:
        im = im.rotate(info["ang"], resample=Image.BICUBIC,
                       center=(info["cx"], info["cy"]), fillcolor="white")   # 整数 255 在 RGB 图上会补出红边
        rotated = True
    tw, th = round(pw / 25.4 * dpi), round(ph / 25.4 * dpi)
    W, H = im.size
    if info and info.get("ax") is not None and not rotated:
        left, top = info["ax"], info["ay"]              # 轴对齐：按检测框左上角
    elif info and info.get("cx") is not None:
        left, top = round(info["cx"] - tw / 2), round(info["cy"] - th / 2)
    else:
        left, top = 0, 0                                # 没识别信息：按原点角(纸都是靠角放/靠边送的)
    left = max(0, min(left, max(0, W - tw)))
    top = max(0, min(top, max(0, H - th)))
    box = im.crop((left, top, left + min(tw, W - left), top + min(th, H - top)))
    if box.size != (tw, th):                            # 比目标小 → 补白边，不拉伸不丢内容
        canvas = Image.new(im.mode, (tw, th), "white")   # ⚠️ 整数 255 在 RGB 里是纯红(255,0,0)
        canvas.paste(box, (0, 0))
        box = canvas
    box.save(png_path, dpi=(dpi, dpi))
    return rotated


def crop_pages(pages, dpi):
    """整批统一：逐页识别(认不出就用该页自身尺寸吸附，因为 ADF 扫出来Full area往往就是纸本身)
    → 投票出统一尺寸 → 每页归一到该尺寸。返回展示用标签。"""
    from PIL import Image
    infos, fallbacks = [], []
    for p in pages:
        try:
            info = analyze_page(p, dpi)
        except Exception:
            info = None
        infos.append(info)
        if info is None:
            # 找不到纸边 = Full area都是纸(ADF 常见)。用图像自身尺寸吸附到标准尺寸
            w, h = Image.open(p).size
            fallbacks.append(snap_size(w / dpi * 25.4, h / dpi * 25.4, tol=8.0))
        else:
            fallbacks.append(None)
    votes = {}
    for info, fb in zip(infos, fallbacks):
        key = (info["name"], info["pw"], info["ph"]) if info else (fb if fb else None)
        if key:
            votes[key] = votes.get(key, 0) + 1
    if not votes:
        return "Full area"                                    # 实在认不出 → 不动(安全兜底)
    # 票数优先；同票取面积大的，宁可多留边也不切掉内容
    (name, pw, ph), n = max(votes.items(), key=lambda kv: (kv[1], kv[0][1] * kv[0][2]))
    rot_any = False
    for p, info in zip(pages, infos):
        use = info if (info and (info["name"], info["pw"], info["ph"]) == (name, pw, ph)) else None
        try:
            rot_any |= fit_page(p, dpi, pw, ph, use)
        except Exception:
            pass
    label = name + (" / aligned" if rot_any else "")
    if n < len(pages):
        label += f"（{n}/{len(pages)} pages identified; the remainder use the same size)"
    return label


def cleaner():
    """清理过期文件 + 异常中断留下的孤儿目录。整个循环包异常——线程死了就永远不清理了。
    - 正在扫描时，当前扫描的 .work-* 目录绝不删(长时间后处理/OCR 时目录 mtime 不会更新)。
    - 打印作业目录按"最后一次被访问"计时(预览/打印都会刷新它)，用户还在选页就不会被删。"""
    while True:
        try:
            now = time.time()
            for p in glob.glob(os.path.join(SCANS, "scan-*.pdf")):
                if now - os.path.getmtime(p) > RETAIN_SEC:
                    os.remove(p)
            for p in glob.glob(os.path.join(SCANS, "scan-*.pdf.quality.json")):
                if not os.path.exists(p[:-len(".quality.json")]) and now - os.path.getmtime(p) > RETAIN_SEC:
                    os.remove(p)
            for p in glob.glob(os.path.join(SCANS, ".small-scan-*.pdf")):   # e-mail copy: goes with its PDF
                if (not os.path.exists(os.path.join(SCANS, os.path.basename(p)[len(".small-"):]))
                        or now - os.path.getmtime(p) > RETAIN_SEC):
                    os.remove(p)
            for d in glob.glob(os.path.join(SCANS, ".raw-scan-*")):     # kept originals: go with their PDF
                pdf = os.path.join(SCANS, os.path.basename(d)[len(".raw-"):] + ".pdf")
                if not os.path.exists(pdf) or now - os.path.getmtime(d) > RETAIN_SEC:
                    shutil.rmtree(d, ignore_errors=True)
            active = scanner.get("workdir") if scan_lock.locked() else None
            for d in (glob.glob(os.path.join(JOBS, "*")) + glob.glob(os.path.join(SCANS, ".work-*"))
                      + glob.glob(os.path.join(SCANS, ".part-*"))):
                if d == active or os.path.basename(d) == "tmp":        # tmp = 容器 TMPDIR，别删
                    continue
                if os.path.basename(d).startswith(".part-") and scan_lock.locked():
                    continue                                          # 正在生成/OCR 的那份
                if now - os.path.getmtime(d) > RETAIN_SEC:
                    shutil.rmtree(d, ignore_errors=True) if os.path.isdir(d) else os.remove(d)
            # 容器 TMPDIR(= JOBS/tmp) 里被杀掉的进程(OSD/LibreOffice/ocrmypdf)留下的临时文件：放一天再清
            for t in glob.glob(os.path.join(JOBS, "tmp", "*")):
                if now - os.path.getmtime(t) > 24 * 3600:
                    shutil.rmtree(t, ignore_errors=True) if os.path.isdir(t) else os.remove(t)
        except Exception as e:
            log_event("cleaner-error", e)
        time.sleep(30)


def _device_dpi(pages, res):
    """扫描仪**实际**给的 DPI(读第一页 PNG 元数据)。返回 (dpi 字符串, 给用户的说明)。
    WSD 会把超出能力的请求静默降级(请求 300 给 200，元数据里如实写着 200)——
    按请求值标注会把所有尺寸几何一起算错，所以正常路径和超时抢救路径都必须走这里。"""
    try:
        from PIL import Image
        with Image.open(pages[0]) as im:
            real = (im.info.get("dpi") or (0,))[0]
        if real and abs(real - int(res)) > 2:
            log_event("scan-res-downgrade",
                      "requested %s, device delivered %.0f — using %.0f" % (res, real, real))
            return str(int(round(real))), "; scanner returned %.0f dpi (requested %s)" % (real, res)
    except Exception:
        pass
    return res, ""


def _stamp_dpi(pages, res):
    """强制写 DPI 元数据：缺了 img2pdf 会按 96dpi 算出 308x438mm 这种怪页面。"""
    from PIL import Image
    for p in pages:
        try:
            with Image.open(p) as im:
                im.load()
                im.save(p, dpi=(res, res))
        except Exception:
            pass


def _as_jpeg(pages, quality=90):
    """彩色/灰度页转 JPEG 再进 PDF(img2pdf 原样嵌入，不再二次压缩)。
    ⚠️ 早期整页 PNG 无损入 PDF：300dpi 彩色 A4 一页 10~15MB，10 页合同发不了邮件/企业微信(C04)。
    quality 90 + 4:4:4 色度不抽样：红章、蓝签字的细笔画不会糊。DPI 元数据照写(页面几何不变)。
    转不了的页原样用 PNG——宁可大一点，不能丢页。"""
    from PIL import Image
    out = []
    for p in pages:
        try:
            with Image.open(p) as im:
                if im.mode not in ("RGB", "L"):
                    out.append(p)
                    continue
                dpi = im.info.get("dpi") or (300, 300)
                jp = os.path.splitext(p)[0] + ".jpg"
                im.save(jp, "JPEG", quality=quality, subsampling=0,
                        dpi=(int(round(dpi[0])), int(round(dpi[1]))))
            out.append(jp)
        except Exception:
            out.append(p)
    return out


def _read_scan_quality(pdf):
    try:
        with open(str(pdf) + ".quality.json", encoding="utf-8") as handle:
            value = json.load(handle)
        if (value.get("state") in ("done", "review", "partial")
                and isinstance(value.get("issues"), list)
                and all(isinstance(item, str) for item in value["issues"])):
            return value
    except FileNotFoundError:
        return {"state": "review", "issues": ["The scan-check record is missing. Compare the original with the PDF to verify completeness."]}
    except (OSError, ValueError, AttributeError):
        pass
    return {"state": "review", "issues": ["The scan-check record could not be read. Compare the original with the PDF."]}


def _write_scan_quality(pdf, quality):
    target = str(pdf) + ".quality.json"
    temporary = target + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(quality, handle, ensure_ascii=False)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _publish_pdf(pages, out, do_ocr=False, jid=None, n=0, quality=None):
    """页面 → PDF → (可选 OCR) → **最后一步才原子改名成 scan-*.pdf**。
    ⚠️ 早期直接往最终文件名里写：写到一半/OCR 前的文件就出现在列表里被人下载；OCR 期间删掉的文件
    还会被 OCR 结果"复活"；OCR 跑了十几分钟后文件 mtime 早就过期，一发布 30 秒内就被清理掉。
    返回 OCR 错误说明(None=没做或成功)。"""
    part = os.path.join(SCANS, ".part-" + os.path.basename(out))
    small_part = os.path.join(SCANS, ".part-small-" + os.path.basename(out))
    oerr = None
    try:
        # Since 2026-10-19 the PDF is lossless: the scanner's PNG pages are embedded unchanged (no JPEG conversion).
        # A separate JPEG q90 copy is made for e-mail/WeChat (C04: lossless pages are ~3 MB each, a 10-page contract
        # would not go through e-mail).
        with open(part, "wb") as f:
            try:
                subprocess.run(["img2pdf"] + list(pages), stdout=f, check=True, timeout=300)
            except subprocess.TimeoutExpired:
                # 不能原样往上抛：run_scan 会把它当成"扫描仪卡住"去报，原因就说错了
                raise RuntimeError("PDF generation timed out. Lower the resolution or scan in smaller batches")
        try:
            with open(small_part, "wb") as f:
                subprocess.run(["img2pdf"] + _as_jpeg(pages), stdout=f, check=True, timeout=300)
        except Exception as exc:                        # a failed e-mail copy never affects the PDF itself
            log_event("scan-small-copy-fail", "%s: %s" % (type(exc).__name__, str(exc)[:150]))
            if os.path.exists(small_part):
                os.remove(small_part)
        if do_ocr:
            if jid:
                job_update(jid, msg="Recognizing text... (%d pages, about %d seconds)" % (n, max(5, n * 5)))
            oerr = ocr_pdf(part)
        if quality is not None:
            # A failed OCR is not "content may be missing" (every page is there, just not searchable); the done message
            # already says so, so the scan is not escalated to "needs a check".
            try:
                _write_scan_quality(out, quality)
            except OSError as exc:
                # Preserve the finished scan. Missing metadata is read as review,
                # so persistence failure cannot turn this file green on refresh.
                quality["issues"].append("The scan-check record could not be saved. Compare the original with the PDF.")
                quality["state"] = "review"
                log_event("scan-quality-save-fail", str(exc)[:200])
        if os.path.exists(small_part):
            # offer the e-mail copy only when it really is smaller (a clean, flat page can compress better as PNG)
            if os.path.getsize(small_part) < 0.8 * os.path.getsize(part):
                os.replace(small_part, _small_path(os.path.basename(out)))
                os.utime(_small_path(os.path.basename(out)))   # expires with the PDF, not from before a slow OCR
            else:
                os.remove(small_part)
        os.replace(part, out)
        os.utime(out)                                   # 从发布那一刻起算 15 分钟
    finally:
        for leftover in (part, small_part):
            if os.path.exists(leftover):
                os.remove(leftover)
    return oerr


def _small_path(pdf_name):
    """The e-mail sized copy (JPEG q90) of scan-...pdf; expires and is deleted with it."""
    return os.path.join(SCANS, ".small-" + pdf_name)


def _raw_dir(pdf_name):
    """Folder (.raw-scan-...-Np) holding the untouched frames of scan-...-Np.pdf; expires and is deleted with it."""
    return os.path.join(SCANS, ".raw-" + pdf_name[:-4])


def _keep_raw(pages, workdir):
    """Copy the scanner's frames before any processing; they are published next to the PDF for the same 15 minutes.
    Only used when processing was requested. Before 2026-10-19 processing overwrote the frames in place and the work
    folder was deleted, so a lost seal, a wrong rotation or a wrong size could not be checked or redone."""
    raw = os.path.join(workdir, "raw")
    try:
        os.makedirs(raw, exist_ok=True)
        for p in pages:
            shutil.copy2(p, os.path.join(raw, os.path.basename(p)))
        return raw
    except OSError as exc:
        log_event("scan-raw-keep-fail", "%s: %s" % (type(exc).__name__, str(exc)[:150]))
        return None


def _publish_raw(raw_tmp, pdf_name, record):
    """Move the kept frames plus the processing record (record.json) to .raw-<PDF name>, on the PDF's clock.
    A failure is only logged and never affects the PDF."""
    if not raw_tmp or not os.path.isdir(raw_tmp):
        return
    target = _raw_dir(pdf_name)
    try:
        with open(os.path.join(raw_tmp, "record.json"), "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=1, default=str)
        if os.path.exists(target):
            shutil.rmtree(target, ignore_errors=True)
        os.replace(raw_tmp, target)
        os.utime(target)
    except OSError as exc:
        log_event("scan-raw-keep-fail", "%s: %s" % (type(exc).__name__, str(exc)[:150]))


def recover_orphan_workdirs():
    """启动时把上次没做完的扫描(进程被 OOM 杀掉/容器重启)里已经扫好的页合成 PDF 交出来。
    ⚠️ 早期这些页只会在 15 分钟后被 cleaner 静默删掉——用户那一叠等于白扫。
    只做最轻的 img2pdf(不做后处理，免得同一个大页再把内存打爆)。"""
    for d in sorted(glob.glob(os.path.join(SCANS, ".work-*"))):
        try:
            got = _readable_pages(_scan_pages(d))
            if got:
                m = re.match(r"\.work-(\d{8}-\d{6})", os.path.basename(d))
                name = "scan-%s-%dp.pdf" % (m.group(1) if m else sgt(), len(got))
                if not os.path.exists(os.path.join(SCANS, name)):
                    _publish_pdf(got, os.path.join(SCANS, name), quality={"state": "partial",
                                 "issues": ["This partial scan was recovered after a restart. Check its page count, orientation and contents."]})
                    log_event("scan-recovered", "%s from %s" % (name, os.path.basename(d)))
        except Exception as e:
            log_event("scan-recover-fail", "%s %s" % (os.path.basename(d), e))
        finally:
            shutil.rmtree(d, ignore_errors=True)
    for p in glob.glob(os.path.join(SCANS, ".part-*")):
        try:
            os.remove(p)
        except OSError:
            pass


SCAN_STALL_SEC = 180      # 这么久一页都没多出来 = 卡住了
SCAN_HARD_SEC = 3600      # 一批最长 1 小时


def _scan_pages(workdir):
    """已经落盘的页(0 字节的不算——平板扫失败时 scanimage 会留下一个空的 p001.png)。"""
    out = []
    for p in sorted(glob.glob(os.path.join(workdir, "p*.png"))):
        try:
            if os.path.getsize(p) > 0:
                out.append(p)
        except OSError:
            pass
    return out


def _readable_pages(pages):
    """超时被打断时最后一页可能只写了一半——打不开的页不能拿去合成 PDF(否则整份都丢)。"""
    from PIL import Image
    good = []
    for p in pages:
        try:
            with Image.open(p) as im:
                im.load()
            good.append(p)
        except Exception:
            pass
    return good


def _run_scanimage(cmd, workdir):
    """跑 scanimage。超时按"多久没出新页"算(SCAN_STALL_SEC)，不按整批总时长——
    ⚠️ 早期固定 900 秒总时长：一大叠 ADF 正常扫着也会被半路杀掉。返回 (返回码, stderr)。"""
    import signal
    errpath = os.path.join(workdir, "scanimage.err")
    with open(errpath, "w+") as errf:
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=errf, start_new_session=True)
        start = last = time.time()
        seen = 0
        while p.poll() is None:
            time.sleep(1)
            k = len(_scan_pages(workdir))
            now = time.time()
            if k != seen:
                seen, last = k, now
            if now - last > SCAN_STALL_SEC or now - start > SCAN_HARD_SEC:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
                p.wait()
                raise subprocess.TimeoutExpired(cmd, SCAN_STALL_SEC)
        errf.seek(0)
        return p.returncode, errf.read()


ADF_WAIT_SEC = 30       # how long to wait for paper when the feeder reports empty at the start (asks every ADF_WAIT_POLL s)
ADF_WAIT_POLL = 2.5
ADF_FED_SEC = 3.0        # while waiting: an attempt running longer than this has started pulling a sheet ("no paper"
                         # answers return after ~1-2 s because _run_scanimage polls once a second; a page takes >= 4 s)


def _group_by_page(pairs, pages):
    """[(page file, sentence)] -> ["第 1–3 页：sentence", ...]: pages with the same sentence are merged; page numbers
    are those of the final PDF (after blank pages were removed)."""
    groups = {}
    for p, text in pairs:
        if p in pages and text:
            groups.setdefault(text, set()).add(pages.index(p) + 1)
    return ["Pages %s: %s" % (compress_ranges(sorted(v)), t) for t, v in groups.items()]


def run_scan(jid, source, res, mode, size="full", drop_blank=False, do_ocr=False,
             enhance=True, deskew=False, paper_white=False):
    """由持有 scan_lock 的工作线程调用；只更新作业 jid 自己的状态。
    整个函数体都在 try 里——早期异常路径没收尾，站点永远显示"Scanning..."且所有人 409。"""
    workdir = None
    ticking = {"on": False}
    res_note = ""
    try:
        stamp = sgt()
        workdir = os.path.join(SCANS, ".work-%s-%s" % (stamp, jid[:6]))
        scanner.update(pages=0, job=jid, workdir=workdir, phase="scanning")
        job_update(jid, state="running", msg="Preparing the scanner...")
        os.makedirs(workdir, exist_ok=True)
        log_event("scan-start", "%s res=%s mode=%s size=%s blank=%s ocr=%s"
                  % (source, res, mode, size, drop_blank, do_ocr))
        # Size: "full" = the whole glass / the whole feed path; a chosen size = that size's window (the feeder uses the
        # measured adf_window). The scanner's output is delivered as is (since 2026-10-19).
        if size in ("auto", "full"):
            gw, gh = BED_W, BED_H
        else:
            gw, gh = next(((sw, sh) for n, sw, sh in STD_SIZES if n == size), (BED_W, BED_H))
        multipage = source.startswith("ADF")
        # 优先 smfp（真 300/600、快 4 倍），失败回退 WSD（钳 200）
        sdev = smfp_device()
        if sdev:
            # smfp 的几何选项是整型：给 "297.0" 会报 illegal unit (rest of option: .0)
            if multipage:
                # Feeder: "full" scans the whole feed path (0..297 x 0..432 mm, any sheet complete); a chosen size uses
                # the measured window (adf_window: centred on 150 mm, 5 mm extra per side). The old centred window +
                # crop back to 210 mm cut paper edges and sideways sheets while reporting "done" (2026-10-19).
                win = [0, 0, ADF_WIN_W, ADF_WIN_H] if size in ("full", "auto") else adf_window(gw, gh)
                geom = ["-l", str(win[0]), "-t", str(win[1]), "-x", str(win[2]), "-y", str(win[3])]
            else:
                # Flatbed: "full" = the whole glass; a chosen size = the same window HP's driver uses, from the corner
                geom = ["-l", "0", "-t", "0", "-x", str(int(round(gw))), "-y", str(int(round(gh)))]
            base = ["scanimage", "-d", sdev, "--doc-source", SMFP_SRC[source],
                    "--resolution", res, "--mode", SMFP_MODE[mode], "--format=png"] + geom
        wres = res if int(res) <= 200 else "200"        # WSD 通道上限
        base_wsd = ["scanimage", "-d", DEV, "--source", source,
                    "--resolution", wres, "--mode", mode, "--format=png",
                    "-l", "0", "-t", "0", "-x", str(gw), "-y", str(gh)]
        if not sdev:
            base = base_wsd
            log_event("scan-smfp-unavailable", "no M437 ip in scanimage -L; using airscan fallback")
        tail_args = (["--batch=" + os.path.join(workdir, "p%03d.png")] if multipage
                     else ["-o", os.path.join(workdir, "p001.png")])
        cmd = base + tail_args
        cmd_wsd = base_wsd + tail_args
        job_update(jid, msg="Scanning...")

        # 扫描过程中实时报页数——ADF 扫一叠要很久，没进度用户会以为卡死
        def tick():
            while ticking["on"]:
                time.sleep(2)
                try:
                    k = len(_scan_pages(workdir))
                    if k and ticking["on"]:
                        scanner["pages"] = k
                        job_update(jid, msg="Scanning... %d pages scanned" % k)
                except Exception:
                    return
        ticking["on"] = True
        threading.Thread(target=tick, daemon=True).start()
        err = ""
        attempts = 0
        wait_until = None
        while True:
            for z in glob.glob(os.path.join(workdir, "p*.png")):
                if z not in _scan_pages(workdir):
                    os.remove(z)                         # 上一次失败留下的 0 字节空文件
            fed = None
            if wait_until is not None:
                # While waiting for paper the page says "push the stack in". An attempt still running after
                # ADF_FED_SEC is pulling a sheet (a "no paper" answer takes 0.2 s): say so, so nobody pushes it.
                fed = threading.Timer(ADF_FED_SEC, job_update, args=(jid,), kwargs={"msg": "Paper detected; scanning..."})
                fed.daemon = True
                fed.start()
            try:
                rc, stderr = _run_scanimage(cmd, workdir)
            finally:
                if fed:
                    fed.cancel()
            if _scan_pages(workdir):
                err = (stderr or "").strip() if rc not in (0, 7) else ""
                break                                    # rc=7 = ADF 送完最后一页，属正常
            err = (stderr or "").strip()
            low = err.lower()
            if multipage and ("out of documents" in low or "no documents" in low):
                # A feeder may report no paper until its input sensor detects the stack:
                # the sheet was not pushed in far enough for the sensor - the printer's screen changes once it is).
                # Do not fail at once: keep asking (the "no paper" answer takes 0.2 s) and start as soon as paper is in.
                now = time.time()
                if wait_until is None:
                    wait_until = now + ADF_WAIT_SEC
                    log_event("scan-wait-paper", "feeder empty at start; waiting up to %ds" % ADF_WAIT_SEC)
                if now < wait_until:
                    job_update(jid, msg="Waiting for feeder paper. Insert the stack face up, short edge first, until detected; scanning starts automatically (%d seconds remaining). For paper on the glass, wait for this attempt to end and choose Flatbed"
                                        % max(1, int(round(wait_until - now))))
                    time.sleep(ADF_WAIT_POLL)
                    continue
                break
            attempts += 1
            if "busy" in low and attempts < 3:
                job_update(jid, msg="Scanner busy; retrying in %d seconds (%d/3)..." % (6, attempts))
                time.sleep(6)
                continue
            # smfp cannot open (driver/port/IP problem) -> retry over WSD so the station keeps working
            if sdev and cmd is not cmd_wsd and attempts < 3 and ("open of device" in err or "Invalid argument" in err):
                log_event("scan-smfp-fallback", err[:120])
                cmd = cmd_wsd
                continue
            break
        ticking["on"] = False
        pages = _readable_pages(_scan_pages(workdir))
        if not pages:
            job_update(jid, state="error",
                       msg="Error: " + friendly(err or "No pages were scanned. Check the paper and covers"))
            log_event("scan-fail", err)
            return
        scanner["pages"] = len(pages)
        # 分辨率以设备实际返回为准，绝不用请求值标注几何
        res, res_note = _device_dpi(pages, res)
        dpi = int(res)
        # Keep a copy of the frames only when processing was requested: a plain PDF already IS the original
        processing = bool(deskew or enhance or drop_blank or do_ocr or size == "auto" or paper_white)
        raw_tmp = _keep_raw(pages, workdir) if processing else None
        record = {"params": {"source": source, "res": res, "mode": mode, "size": size, "deskew": deskew,
                             "enhance": enhance, "drop_blank": drop_blank, "ocr": do_ocr, "paper_white": paper_white},
                  "device": "wsd" if (not sdev or cmd is cmd_wsd) else "smfp", "dpi": dpi,
                  "geometry_args": [a for a in cmd if not a.startswith("--batch") and not a.startswith("/")][-8:],
                  "pages": {os.path.basename(p): {"raw": os.path.basename(p)} for p in pages}}
        page_issues, page_notes = [], []                # needs a check (content may be missing) / plain notes
        scanner["phase"] = "processing"                 # 扫描仪其实空了，但还在处理这份(内存有限，不并行)
        extra = res_note
        # 中途出错但已扫到几页：如实说明是"部分成功"，绝不冒充完成
        partial = friendly(err) if err else ""
        label = SCAN_SIZES.get(size, size)
        # The scanner's own output (since 2026-10-19): no crop, no size snapping; only the DPI is written so img2pdf
        # computes the page size. Feeder pages get the read-only edge check; the image is never changed.
        _stamp_dpi(pages, dpi)
        adf_label, edge_flagged = None, set()           # edge_flagged: pages the edge check had something to say about
        if multipage and sdev and cmd is not cmd_wsd:
            win = [0, 0, ADF_WIN_W, ADF_WIN_H] if size in ("full", "auto") else adf_window(gw, gh)
            for p in pages:
                try:
                    # a window as wide as the feed path (Full area, A3): touching both side strips is inherent
                    iss, nts = edge_check(p, dpi, whole_path=win[2] >= ADF_WIN_W)
                    page_issues.extend((p, s_) for s_ in iss)
                    page_notes.extend((p, s_) for s_ in nts)
                    if iss or nts:
                        edge_flagged.add(p)
                except Exception as exc:
                    # an unchecked page is never passed as complete
                    edge_flagged.add(p)
                    page_issues.append((p, "The edge check failed. Check all four edges yourself"))
                    log_event("scan-edge-check-fail", "%s: %s" % (type(exc).__name__, str(exc)[:150]))
            adf_label = ("Full area" if size in ("full", "auto") else size, win[2], win[3])
            label = "Feeder / %s (page %d x %d mm)" % adf_label
        elif multipage:
            label = "Feeder / " + ("Full area" if size in ("full", "auto") else size)
        else:
            label = "Flatbed / " + ("Entire glass" if size in ("full", "auto") else size)
        scanned = len(pages)
        if paper_white:
            job_update(jid, msg="Whitening paper...")
            n_white = 0
            for p in pages:
                try:
                    info = whiten_paper(p, dpi, feeder=multipage)
                except Exception as exc:
                    info = {"kept": "failed: %s" % type(exc).__name__}
                    log_event("scan-paper-white-fail", "%s: %s" % (type(exc).__name__, str(exc)[:150]))
                record["pages"][os.path.basename(p)]["paper_white"] = info
                if "gain" in info:
                    n_white += 1
                elif info.get("kept") == "coloured paper":
                    page_notes.append((p, "Colored paper; original color retained"))
                elif str(info.get("kept", "")).startswith("failed"):
                    page_notes.append((p, "Paper background correction failed; original retained"))
            if n_white:
                extra += "; paper background corrected (original scan saved separately)"
        if deskew or enhance:
            # Directions are decided for the whole batch, then rotate + darken run per page in one read/write
            # (an early version saved three full-size PNGs per page and users found it too slow)
            rots = None
            if deskew:
                job_update(jid, msg="Checking page orientation...")
                try:
                    rots = decide_orientations(pages)
                except Exception as exc:
                    log_event("scan-orient-fail", "batch %s: %s" % (type(exc).__name__, str(exc)[:150]))
            n_rot = n_enh = n_bad = 0
            for i, p in enumerate(pages):
                job_update(jid, msg="Processing pages... %d/%d" % (i + 1, len(pages)))
                kw, oerr_i = {}, ""
                if rots is not None:
                    rot_i, note_i, oerr_i = rots[i]
                    kw = {"rotation": rot_i}
                    record["pages"][os.path.basename(p)]["rotation"] = rot_i
                    if note_i:
                        page_notes.append((p, note_i))
                try:
                    r, e, probs = postprocess_page(p, dpi, deskew, enhance, **kw)
                except Exception as ex:
                    r, e, probs = 0, False, ["%s: %s" % (type(ex).__name__, ex)]
                if oerr_i:
                    probs = [oerr_i] + list(probs)
                n_rot += 1 if r else 0
                n_enh += 1 if e else 0
                if probs:
                    n_bad += 1
                    log_event("scan-postprocess-fail", "p%d %s" % (i + 1, "; ".join(probs)[:300]))
            if n_rot:
                extra += "; rotated %d pages" % n_rot
            if n_enh:
                extra += "; text enhanced"
            if n_bad:
                extra += "; orientation or enhancement failed on %d pages (originals retained)" % n_bad
        if drop_blank:
            job_update(jid, msg="Removing blank pages...")
            kept_before = list(pages)
            pages, ndrop = drop_blank_pages(pages, workdir)
            if ndrop:
                extra += "; removed %d blank pages" % ndrop
            for p in kept_before:
                if p not in pages:
                    record["pages"][os.path.basename(p)]["dropped_as_blank"] = True
        if adf_label and not edge_flagged.intersection(pages):
            # Only claimed when every delivered page passed the edge check: the sheet stayed clear of both side
            # strips, so both side edges (where cross-page seals sit) are whole. The leading ~2 mm never is.
            label = "Feeder / %s (page %d x %d mm, both side edges complete)" % adf_label
        notes = _group_by_page(page_notes, pages)
        if notes:
            extra += "。" + "；".join(notes)
        quality_issues = _group_by_page(page_issues, pages)
        # what to do about edge problems, said once after the page list
        advice = "。" + EDGE_ADVICE if any(p in pages and not s_.startswith("Paper edges could not be checked")
                                          for p, s_ in page_issues) else ""
        if partial:
            quality_issues.append("Scan interrupted: " + partial)
        quality = {"state": "partial" if partial else "review" if quality_issues else "done",
                   "issues": quality_issues}
        n = len(pages)
        base_name = "scan-%s-%dp.pdf" % (stamp, n)
        out = os.path.join(SCANS, base_name)
        job_update(jid, msg="Generating PDF...")
        oerr = _publish_pdf(pages, out, do_ocr, jid, n, quality=quality)
        for i, p in enumerate(pages, 1):
            record["pages"][os.path.basename(p)]["pdf_page"] = i
        record.update(quality=quality, label=label, notes=_group_by_page(page_notes, pages))
        _publish_raw(raw_tmp, base_name, record)
        try:
            mb = os.path.getsize(out) / 1024 / 1024
            label += " · %s" % ("%.1f MB" % mb if mb >= 1 else "%d KB" % max(1, mb * 1024))
        except OSError:
            pass
        if do_ocr:
            extra += "; searchable text added" if not oerr else "; OCR failed (%s); the PDF is still usable" % oerr[:40]
        if partial:
            more = [s for s in quality["issues"] if not s.startswith("Scan interrupted: ")]
            job_update(jid, state="partial", file=base_name,
                       msg="Partial result: scanned %d pages into %s (%s%s), but scanning stopped: %s. Check whether pages remain unscanned%s"
                           % (scanned, base_name, label, extra, partial,
                              (". Also: " + "；".join(more) + advice) if more else ""))
            log_event("scan-partial", "%s pages=%d %s err=%s" % (base_name, scanned, label, err))
        elif quality["state"] == "review":
            job_update(jid, state="review", file=base_name,
                       msg="Review required: %s (%d pages / %s%s). %s%s" % (base_name, n, label, extra, "；".join(quality["issues"]), advice))
            log_event("scan-review", "%s pages=%d %s%s | %s" % (base_name, n, label, extra, "；".join(quality["issues"])))
        else:
            job_update(jid, state="done", file=base_name,
                       msg="Complete: %s (%d pages / %s%s; deleted automatically after 15 minutes)" % (base_name, n, label, extra))
            log_event("scan-done", "%s pages=%d %s%s" % (base_name, n, label, extra))
    except subprocess.TimeoutExpired:
        # 超时不能把已扫好的页销毁——那等于白扫一叠。有几页就交几页(补 DPI)，并说明是部分结果
        ticking["on"] = False
        saved, fname = "", ""
        try:
            got = _readable_pages(_scan_pages(workdir)) if workdir else []
            if got:
                _stamp_dpi(got, int(_device_dpi(got, res)[0]))
                fname = "scan-%s-%dp.pdf" % (sgt(), len(got))
                _publish_pdf(got, os.path.join(SCANS, fname), quality={"state": "partial",
                             "issues": ["Scanning timed out. Only completed pages were saved; check count, orientation and contents."]})
                saved = "; saved %d completed pages as %s" % (len(got), fname)
        except Exception as e:
            log_event("scan-timeout-salvage-fail", e)
            fname = ""
        job_update(jid, state="partial" if fname else "error", file=fname,
                   msg="Error: No new page arrived for %d seconds, so scanning stopped. Check the panel for a jam or sleep mode%s"
                       % (SCAN_STALL_SEC, saved))
        log_event("scan-timeout", saved)
    except Exception as e:
        job_update(jid, state="error", msg="Error: " + friendly(e))
        log_event("scan-error", e)
    finally:
        ticking["on"] = False                           # 任何路径都要停掉进度线程
        scanner.update(pages=0, workdir=None, phase="")   # job 留着：锁放掉前 /status 仍认得出是本人的扫描
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)


PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>Office Scan & Print Station</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='6' fill='%232563eb'/%3E%3Cpath d='M7 9V4h10v5M7 17H5a2 2 0 0 1-2-2v-4a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2h-2M7 14h10v6H7z' fill='none' stroke='%23fff' stroke-width='1.8' stroke-linejoin='round'/%3E%3C/svg%3E">
<!--
  Office scan + print station page (served by scan_app.py; one self-contained file, no external resources).
  The server injects the boot data (option lists, current files, limits) into the script below.
  Talks to the backend only with fetch (GET /status /files /preview/<token> /f/<name>;
  POST /scan /upload /copy /print /cancel /delete). Contract: impl/API.md.
  window.Station exposes the UI hooks (setTab, setScanState, setFiles, loadDocument, setConverting,
  setPrintState, setPaperHint, setPagesText, setDeviceChip, parseRange, formatRange, selectedPages).
-->
<style>
:root{
  color-scheme: light dark;
  --font: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", system-ui, "Segoe UI", sans-serif;
  --bg:#f3f4f7; --bg-tint:#e6edfb;
  --surface:#ffffff; --surface-2:#f7f8fa; --surface-3:#eceef2; --field:#ffffff;
  --border:#e3e6eb; --border-strong:#cfd4dc;
  --text:#111827; --text-2:#4a5260; --text-3:#646c79;
  --accent:#2563eb; --accent-ink:#1d4ed8; --accent-soft:#edf3fe; --accent-soft-2:#d5e3fd;
  --accent-btn:#2563eb; --accent-btn-hover:#1d4ed8;
  --btn-off:#d5dae2; --btn-off-ink:#5f6775;
  --tab-on:#ffffff; --on-tone:#ffffff;
  --ok:#15803d; --ok-soft:#ecf8f0; --ok-line:#bfe3cc;
  --warn:#b45309; --warn-soft:#fef6e8; --warn-line:#f4d8a6;
  --err:#c42b1c; --err-soft:#fdefed; --err-line:#f3c4be;
  --focus: rgba(37,99,235,.45);
  --paper:#ffffff; --paper-ink:#9aa3b0; --paper-line:#dde1e7; --paper-num:#aab2bd;
  --paper-off:#eceef2; --off-line:#aeb6c2; --off-op:.22;
  --stamp-ink:#4f5866; --stamp-line:#7f8896; --stamp-bg:rgba(255,255,255,.92);
  --head-bg:rgba(255,255,255,.72);
  --shadow-sm: 0 1px 2px rgba(16,24,40,.05);
  --shadow: 0 1px 2px rgba(16,24,40,.04), 0 3px 10px rgba(16,24,40,.05);
  --shadow-lg: 0 12px 32px rgba(16,24,40,.12);
  --radius:18px;
  --head-h:0px;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#0e1015; --bg-tint:#131a29;
    --surface:#171a21; --surface-2:#1c2028; --surface-3:#252a33; --field:#12151b;
    --border:#2a2f39; --border-strong:#3a414d;
    --text:#eceef2; --text-2:#b4bbc7; --text-3:#959eab;
    --accent:#7aa5f5; --accent-ink:#9dbcf7; --accent-soft:rgba(91,141,239,.12); --accent-soft-2:rgba(91,141,239,.28);
    --accent-btn:#3a6fe0; --accent-btn-hover:#4a7eee;
    --btn-off:#2b303a; --btn-off-ink:#8a93a1;
    --tab-on:#323845; --on-tone:#10131a;
    --ok:#5fcf8c; --ok-soft:rgba(52,178,100,.12); --ok-line:rgba(95,207,140,.30);
    --warn:#f0b35a; --warn-soft:rgba(240,179,90,.11); --warn-line:rgba(240,179,90,.32);
    --err:#ff8a80; --err-soft:rgba(255,110,100,.11); --err-line:rgba(255,138,128,.34);
    --focus: rgba(122,165,245,.55);
    --paper:#e9ebef; --paper-ink:#8e97a4; --paper-line:#c6ccd4; --paper-num:#9aa2ad;
    --paper-off:#1d2129; --off-line:#4d5563; --off-op:.12;
    --stamp-ink:#c9cfd8; --stamp-line:#8a93a1; --stamp-bg:rgba(20,23,29,.9);
    --head-bg:rgba(23,26,33,.72);
    --shadow-sm: 0 1px 2px rgba(0,0,0,.3);
    --shadow: 0 1px 2px rgba(0,0,0,.28), 0 4px 14px rgba(0,0,0,.22);
    --shadow-lg: 0 14px 36px rgba(0,0,0,.5);
  }
}
:root[data-theme="dark"]{
  --bg:#0e1015; --bg-tint:#131a29;
  --surface:#171a21; --surface-2:#1c2028; --surface-3:#252a33; --field:#12151b;
  --border:#2a2f39; --border-strong:#3a414d;
  --text:#eceef2; --text-2:#b4bbc7; --text-3:#959eab;
  --accent:#7aa5f5; --accent-ink:#9dbcf7; --accent-soft:rgba(91,141,239,.12); --accent-soft-2:rgba(91,141,239,.28);
  --accent-btn:#3a6fe0; --accent-btn-hover:#4a7eee;
  --btn-off:#2b303a; --btn-off-ink:#8a93a1;
  --tab-on:#323845; --on-tone:#10131a;
  --ok:#5fcf8c; --ok-soft:rgba(52,178,100,.12); --ok-line:rgba(95,207,140,.30);
  --warn:#f0b35a; --warn-soft:rgba(240,179,90,.11); --warn-line:rgba(240,179,90,.32);
  --err:#ff8a80; --err-soft:rgba(255,110,100,.11); --err-line:rgba(255,138,128,.34);
  --focus: rgba(122,165,245,.55);
  --paper:#e9ebef; --paper-ink:#8e97a4; --paper-line:#c6ccd4; --paper-num:#9aa2ad;
  --paper-off:#1d2129; --off-line:#4d5563; --off-op:.12;
  --stamp-ink:#c9cfd8; --stamp-line:#8a93a1; --stamp-bg:rgba(20,23,29,.9);
  --head-bg:rgba(23,26,33,.72);
  --shadow-sm: 0 1px 2px rgba(0,0,0,.3);
  --shadow: 0 1px 2px rgba(0,0,0,.28), 0 4px 14px rgba(0,0,0,.22);
  --shadow-lg: 0 14px 36px rgba(0,0,0,.5);
}
@media (min-width:960px){:root{--head-h:84px}}

*,*::before,*::after{box-sizing:border-box}
[hidden]{display:none !important}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{
  margin:0; position:relative; min-height:100vh;
  font-family:var(--font); font-size:15px; line-height:1.55;
  color:var(--text); background:var(--bg);
  -webkit-font-smoothing:antialiased;
}
body::before{content:"";position:absolute;top:0;right:0;left:0;height:440px;background:linear-gradient(180deg,var(--bg-tint),rgba(0,0,0,0));pointer-events:none;z-index:0}
p,h1,h2,h3,ol{margin:0}
p,small,li,.hint,.b-title,.b-detail,.tip,.sumbox{text-wrap:pretty}
button,input,select{font:inherit;color:inherit}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
.sr{position:absolute !important;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}

/* icons */
.i{width:20px;height:20px;flex:none;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}

/* ---------- header (phones: stacked; desktop: sticky bar with centred tabs) ---------- */
.top{position:relative;z-index:40}
.top-in{max-width:1180px;margin:0 auto;padding:20px 16px 0;display:grid;grid-template-columns:minmax(0,1fr);
  grid-template-areas:"brand" "status" "tabs";gap:14px}
.brand{grid-area:brand;display:flex;align-items:center;gap:12px;min-width:0}
.status-area{grid-area:status}
.tabs{grid-area:tabs}
.mark{width:44px;height:44px;border-radius:13px;flex:none;display:grid;place-items:center;color:#fff;
  background:linear-gradient(160deg,#4f8df7,#2359d8);box-shadow:0 4px 12px rgba(37,99,235,.28), inset 0 1px 0 rgba(255,255,255,.25)}
.mark .i{width:24px;height:24px}
.brand-t{min-width:0}
h1{font-size:20px;line-height:1.3;font-weight:700;letter-spacing:.02em;white-space:nowrap}
.brand-sub{font-size:13px;color:var(--text-3);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
@media (min-width:600px) and (max-width:959px){
  .top-in{grid-template-columns:minmax(0,1fr) auto;grid-template-areas:"brand status" "tabs tabs";align-items:center}
}
@media (min-width:960px){
  .top{position:sticky;top:0;background:var(--head-bg);border-bottom:1px solid var(--border);
    -webkit-backdrop-filter:saturate(1.6) blur(18px);backdrop-filter:saturate(1.6) blur(18px)}
  .top-in{padding:0 32px;min-height:var(--head-h);grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);grid-template-areas:"brand tabs status";align-items:center;gap:24px}
  .status-area{justify-self:end}
  h1{font-size:21px}
}
@media (min-width:960px) and (max-width:1199px){.brand-sub{display:none}}

.status-area{display:flex;gap:8px;flex-wrap:wrap}
.chip{display:inline-flex;align-items:center;gap:8px;min-height:34px;padding:0 13px;border-radius:999px;
  background:var(--surface);border:1px solid var(--border);box-shadow:var(--shadow-sm);font-size:13px;color:var(--text-2);white-space:nowrap}
.chip b{font-weight:600;color:var(--text)}
.dot{width:8px;height:8px;border-radius:50%;flex:none;background:var(--ok);box-shadow:0 0 0 3px var(--ok-soft)}
.chip[data-tone=busy] .dot{background:var(--accent);box-shadow:0 0 0 3px var(--accent-soft-2);animation:pulse 1.4s ease-in-out infinite}
.chip[data-tone=wait]{background:var(--warn-soft);border-color:var(--warn-line)}
.chip[data-tone=wait] .dot{background:var(--warn);box-shadow:0 0 0 3px var(--warn-line)}
.chip[data-tone=wait] b{color:var(--warn)}
.chip[data-tone=err]{background:var(--err-soft);border-color:var(--err-line);color:var(--err)}
.chip[data-tone=err] b{color:var(--err)}
.chip[data-tone=err] .dot{background:var(--err);box-shadow:0 0 0 3px var(--err-line)}
.chip[data-tone=idle] .dot{background:var(--text-3);box-shadow:0 0 0 3px var(--surface-3)}
.chip[data-tone=idle] b{color:var(--text-2);font-weight:500}
@media (max-width:599px){
  .status-area{display:grid;grid-template-columns:1fr 1fr}
  .chip{justify-content:center;min-height:36px;padding:0 10px}
}

/* tabs: segmented switcher with a plain-language subtitle */
.tabs{display:grid;grid-template-columns:1fr 1fr;gap:4px;padding:4px;border-radius:18px;background:var(--surface-3);
  box-shadow:inset 0 1px 2px rgba(16,24,40,.06)}
.tab{appearance:none;-webkit-appearance:none;border:0;background:transparent;cursor:pointer;min-height:58px;padding:6px 10px;border-radius:14px;
  display:flex;align-items:center;justify-content:center;gap:10px;text-align:left;color:var(--text-2);
  transition:background-color .2s,color .2s,box-shadow .2s}
.tab:hover{color:var(--text)}
.tab .i{width:22px;height:22px}
.tab-t{display:grid;line-height:1.25;min-width:0}
.tab-t b{font-size:16px;font-weight:650}
.tab-t small{font-size:12.5px;font-weight:500;color:var(--text-3);white-space:nowrap}
.tab[aria-selected=true]{background:var(--tab-on);color:var(--text);box-shadow:0 1px 2px rgba(16,24,40,.08),0 2px 8px rgba(16,24,40,.08)}
.tab[aria-selected=true] .i{color:var(--accent)}
.tab[aria-selected=true] small{color:var(--text-2)}
@media (min-width:960px){.tabs{width:392px}.tab{min-height:52px;padding:4px 12px}}
@media (min-width:960px) and (max-width:1099px){.tabs{width:280px}.tab-t small{display:none}.tab{min-height:44px}}

/* ---------- page shell ---------- */
.wrap{position:relative;z-index:1;max-width:1180px;margin:0 auto;padding:18px 16px 128px;padding:18px 16px calc(128px + env(safe-area-inset-bottom))}
@media (min-width:960px){.wrap{padding:28px 32px 64px}}

/* ---------- cards ---------- */
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);min-width:0}
.card-h{display:flex;align-items:center;justify-content:space-between;gap:6px 12px;flex-wrap:wrap;padding:18px 20px 0}
.card-h h2{font-size:17px;line-height:1.4;font-weight:650;display:flex;align-items:center;gap:8px}
.card-b{padding:18px 20px 22px}
fieldset.card-b{border:0;margin:0;min-width:0;display:block}
@media (max-width:599px){.card-h{padding:16px 16px 0}.card-b,fieldset.card-b{padding:16px 16px 20px}}
.count{display:inline-grid;place-items:center;min-width:22px;height:22px;padding:0 7px;border-radius:999px;background:var(--surface-3);color:var(--text-2);font-size:12.5px;font-weight:600}
.meta-note{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:var(--text-3)}
.meta-note .i{width:16px;height:16px}
.meta-note.is-lock{color:var(--accent-ink);font-weight:600}
.tag{display:inline-flex;align-items:center;gap:6px;min-height:28px;padding:0 10px;border-radius:999px;background:var(--surface-3);color:var(--text-2);font-size:12.5px;font-weight:600}
.tag .i{width:15px;height:15px}

.group + .group{margin-top:22px;padding-top:22px;border-top:1px solid var(--border)}
fieldset{border:0;margin:0;padding:0;min-width:0}
.field{min-width:0}
.field + .field{margin-top:22px}
.field-label{display:flex;align-items:baseline;flex-wrap:wrap;gap:2px 8px;padding:0;margin:0 0 10px;font-size:14.5px;font-weight:600;color:var(--text)}
.field-label .unit{font-size:12.5px;font-weight:500;color:var(--text-3)}
.field-row{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:-8px 0 2px}
.field-row .field-label{margin:0}
.hint{font-size:13px;line-height:1.6;color:var(--text-3);margin-top:8px}
.row2{display:grid;gap:22px}
.row2 > .field{margin-top:0}
.row2 + .field{margin-top:22px}
@media (min-width:600px){.row2{grid-template-columns:1fr 1fr;gap:20px}}

/* disabled (locked while scanning) */
fieldset:disabled .group{opacity:.5;filter:saturate(.5)}
fieldset:disabled label,fieldset:disabled select{cursor:not-allowed}

/* segmented control (radio pills) */
.seg{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;gap:3px;padding:4px;border-radius:13px;background:var(--surface-3)}
.seg label{position:relative;display:block;cursor:pointer}
.seg input{position:absolute;opacity:0;top:0;right:0;bottom:0;left:0;margin:0;pointer-events:none}
.seg span{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:44px;padding:0 4px;border-radius:10px;
  font-size:15px;line-height:1.15;font-weight:550;color:var(--text-2);text-align:center;transition:background-color .15s,color .15s,box-shadow .15s}
.seg label:hover span{color:var(--text)}
.seg input:checked + span{background:var(--tab-on);color:var(--text);font-weight:650;box-shadow:0 1px 2px rgba(16,24,40,.1),0 1px 6px rgba(16,24,40,.06)}
.seg input:focus-visible + span{outline:3px solid var(--focus);outline-offset:1px}
.seg .rec{font-style:normal;font-size:10.5px;line-height:1.2;font-weight:700;color:var(--accent-ink);margin-top:2px;letter-spacing:.04em}
/* two-line options (缩放): short name + what it does */
.seg.two-line span{min-height:56px;padding:6px 6px}
.seg .sub{font-style:normal;font-size:12px;line-height:1.3;font-weight:500;color:var(--text-3);margin-top:3px}
.seg input:checked + span .sub{color:var(--text-2)}

/* option tiles (radio cards with icon) */
.tiles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
.tile{position:relative;display:block;cursor:pointer;min-width:0}
.tile input{position:absolute;opacity:0;top:0;right:0;bottom:0;left:0;margin:0;pointer-events:none}
.tile .t{position:relative;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;height:100%;min-height:100px;
  padding:14px 4px 12px;border:1.5px solid var(--border);border-radius:14px;background:var(--surface);text-align:center;
  transition:border-color .15s,background-color .15s,box-shadow .15s}
.tile:hover .t{border-color:var(--border-strong)}
.tile .t > .i{width:30px;height:30px;color:var(--text-3);margin-bottom:4px;stroke-width:1.6}
.tile b{font-size:14.5px;line-height:1.3;font-weight:650}
.tile small{font-size:12px;line-height:1.35;color:var(--text-3)}
.tile .ck{position:absolute;top:7px;right:7px;width:18px;height:18px;border-radius:50%;border:1.5px solid var(--border-strong);display:grid;place-items:center;background:var(--surface)}
.tile .ck .i{width:12px;height:12px;stroke-width:3;opacity:0;color:#fff}
.tile input:checked + .t{border-color:var(--accent);background:var(--accent-soft);box-shadow:inset 0 0 0 1px var(--accent)}
.tile input:checked + .t > .i{color:var(--accent)}
.tile input:checked + .t .ck{background:var(--accent-btn);border-color:var(--accent-btn)}
.tile input:checked + .t .ck .i{opacity:1}
.tile input:focus-visible + .t{outline:3px solid var(--focus);outline-offset:2px}
.tiles.compact .t{min-height:88px;padding:12px 4px 10px}
.tiles.compact .t > .i{width:26px;height:26px;margin-bottom:2px}
.tiles.compact b{font-size:14px}
@media (max-width:400px){.tile b,.tiles.compact b{font-size:13.5px;letter-spacing:-.01em}}

/* select + text input */
.select{position:relative}
.select select{appearance:none;-webkit-appearance:none;width:100%;min-height:48px;padding:0 42px 0 14px;border:1px solid var(--border-strong);border-radius:12px;
  background:var(--field);font-size:16px;cursor:pointer;transition:border-color .15s,box-shadow .15s;text-overflow:ellipsis}
.select > .i{position:absolute;right:13px;top:50%;transform:translateY(-50%);pointer-events:none;color:var(--text-3);width:18px;height:18px}
.select select:hover{border-color:var(--text-3)}
.select select:focus-visible,.input:focus-visible{outline:none;border-color:var(--accent);box-shadow:0 0 0 4px var(--accent-soft-2)}
.input{width:100%;min-height:48px;padding:0 14px;border:1px solid var(--border-strong);border-radius:12px;background:var(--field);font-size:16px;transition:border-color .15s,box-shadow .15s}
.input::placeholder{color:var(--text-3);opacity:1}
.input:disabled{background:var(--surface-3);cursor:not-allowed}
.input[aria-invalid=true]{border-color:var(--err);box-shadow:0 0 0 4px var(--err-soft)}

/* checkbox cards */
.checks{display:grid;gap:8px}
@media (min-width:600px){.checks{grid-template-columns:1fr 1fr}}
.check{position:relative;display:flex;align-items:flex-start;gap:12px;min-height:60px;padding:12px 14px;border:1px solid var(--border);border-radius:14px;cursor:pointer;
  background:var(--surface);transition:background-color .15s,border-color .15s}
.check:hover{border-color:var(--border-strong)}
.check input{position:absolute;opacity:0;top:0;right:0;bottom:0;left:0;margin:0;pointer-events:none}
.box{flex:none;width:22px;height:22px;margin-top:1px;border-radius:7px;border:1.5px solid var(--border-strong);background:var(--field);display:grid;place-items:center;transition:background-color .15s,border-color .15s}
.box .i{width:14px;height:14px;stroke-width:3;color:#fff;opacity:0}
.check input:checked ~ .box{background:var(--accent-btn);border-color:var(--accent-btn)}
.check input:checked ~ .box .i{opacity:1}
.check input:focus-visible ~ .box{outline:3px solid var(--focus);outline-offset:2px}
.check:has(input:checked){background:var(--accent-soft);border-color:var(--accent-soft-2)}
.check-t{min-width:0}
.check-t b{display:block;font-size:14.5px;line-height:1.4;font-weight:600}
.check-t small{display:block;margin-top:2px;font-size:12.5px;line-height:1.5;color:var(--text-3)}

.tip{display:flex;gap:10px;margin-top:12px;padding:12px 14px;border-radius:12px;background:var(--surface-2);border:1px solid var(--border);color:var(--text-2);font-size:13.5px;line-height:1.65}
.tip > .i{width:18px;height:18px;color:var(--accent);margin-top:2px}
.tip p + p{margin-top:6px}
.tip b{color:var(--text);font-weight:650}
.note-warn{display:flex;gap:8px;align-items:flex-start;margin-top:10px;padding:9px 12px;border-radius:10px;background:var(--warn-soft);border:1px solid var(--warn-line);color:var(--warn);font-size:13.5px;font-weight:600;line-height:1.5}
.note-warn .i{width:17px;height:17px;margin-top:1px}
.note-warn.is-info{background:var(--surface-2);border-color:var(--border);color:var(--text-2);font-weight:500}
.note-warn.is-info .i{color:var(--accent)}

/* ---------- buttons ---------- */
.btn{appearance:none;-webkit-appearance:none;border:0;display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:44px;padding:0 16px;border-radius:12px;
  font-size:15px;font-weight:600;line-height:1.2;text-decoration:none;white-space:nowrap;cursor:pointer;
  transition:background-color .15s,border-color .15s,box-shadow .15s,transform .06s}
.btn:active:not(:disabled){transform:translateY(1px)}
.btn .i{width:18px;height:18px}
.btn-primary{background:var(--accent-btn);color:#fff;box-shadow:0 1px 2px rgba(16,24,40,.14),0 4px 12px rgba(37,99,235,.22),inset 0 1px 0 rgba(255,255,255,.14)}
.btn-primary:hover:not(:disabled){background:var(--accent-btn-hover)}
.btn:disabled{background:var(--btn-off);color:var(--btn-off-ink);border-color:transparent;box-shadow:none;cursor:not-allowed}
.btn.is-busy:disabled{background:var(--accent-btn);color:#fff;opacity:.8;cursor:progress}
.btn-lg{min-height:50px;min-width:140px;padding:0 22px;font-size:16.5px;border-radius:14px}
.btn-secondary{background:var(--surface);color:var(--text);border:1px solid var(--border-strong);box-shadow:var(--shadow-sm)}
.btn-secondary:hover:not(:disabled){background:var(--surface-2)}
.btn-danger{background:transparent;color:var(--err);border:1px solid var(--err-line)}
.btn-danger:hover{background:var(--err-soft)}
.btn-danger.is-armed{background:var(--err);border-color:var(--err);color:var(--on-tone)}
.btn-sm{min-height:40px;padding:0 12px;font-size:14px;border-radius:10px;gap:6px}
.btn-sm .i{width:16px;height:16px}
.link{appearance:none;-webkit-appearance:none;border:0;background:none;cursor:pointer;min-height:44px;padding:0 10px;border-radius:8px;color:var(--accent-ink);font-size:14.5px;font-weight:600}
.link:hover:not(:disabled){background:var(--accent-soft)}
.link:disabled{color:var(--text-3);cursor:not-allowed}
@media (max-width:599px){.btn-sm{min-height:44px}}

.spin{width:18px;height:18px;border-radius:50%;border:2.5px solid currentColor;border-right-color:transparent;animation:spin .8s linear infinite;flex:none}

/* ---------- action bar (card footer on desktop, frosted fixed bar on phones) ---------- */
.actionbar{display:flex;align-items:center;gap:16px;padding:16px 20px;border-top:1px solid var(--border);background:var(--surface-2);border-radius:0 0 var(--radius) var(--radius)}
.sum{flex:1;min-width:0}
.sum-main{font-size:14.5px;font-weight:650;line-height:1.4;overflow-wrap:break-word;word-wrap:break-word;overflow-wrap:anywhere}
.sum-sub{font-size:13px;line-height:1.45;color:var(--text-3);margin-top:1px}
.sum-sub[data-tone=busy]{color:var(--accent-ink)} .sum-sub[data-tone=ok]{color:var(--ok)}
.sum-sub[data-tone=warn]{color:var(--warn)} .sum-sub[data-tone=err]{color:var(--err)}
.sum-sub[data-tone]{font-weight:600}
.bar-acts{display:flex;gap:10px;flex:none}
@media (min-width:960px){
  /* keep Start scan on screen on short laptops: the footer sticks to the viewport bottom */
  .scan-settings .actionbar{position:sticky;bottom:16px;z-index:5;box-shadow:0 12px 28px -14px rgba(16,24,40,.28)}
}
@media (max-width:959px){
  .actionbar{position:fixed;z-index:30;left:0;right:0;bottom:0;border-radius:0;gap:12px;
    padding:12px 16px;
    padding:12px max(16px, env(safe-area-inset-right)) calc(12px + env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left));
    background:var(--surface);
    -webkit-backdrop-filter:saturate(1.6) blur(18px);backdrop-filter:saturate(1.6) blur(18px);
    box-shadow:0 -6px 24px rgba(16,24,40,.08)}
  .sum-main{font-size:14px;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden}
  .sum-sub{font-size:12.5px;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden}
  .actionbar .btn-lg{min-width:118px;padding:0 18px}
}
@supports (background:color-mix(in srgb, red 50%, transparent)){
  @media (max-width:959px){.actionbar{background:color-mix(in srgb, var(--surface) 90%, transparent)}}
  .b-ico{box-shadow:0 3px 10px rgba(16,24,40,.12)}
}
@media (max-width:599px){
  /* two buttons (Download PDF + Scan another) take the whole bar; the banner above already explains */
  .actionbar.two .sum{display:none}
  .actionbar.two .bar-acts{flex:1}
  .actionbar.two .bar-acts .btn{flex:1;min-width:0}
}

/* ---------- status banner ---------- */
.banner{--tone:var(--accent);--soft:var(--accent-soft);--line:var(--accent-soft-2);
  display:grid;grid-template-columns:auto minmax(0,1fr);gap:14px;align-items:start;padding:16px 18px;border-radius:var(--radius);
  background:var(--soft);border:1px solid var(--line);box-shadow:var(--shadow-sm);animation:rise .25s ease-out;
  scroll-margin-top:calc(var(--head-h) + 12px)}
.banner.tone-idle{--tone:var(--accent);--soft:var(--surface);--line:var(--border)}
.banner.tone-busy{--tone:var(--accent);--soft:var(--accent-soft);--line:var(--accent-soft-2)}
.banner.tone-wait{--tone:var(--warn);--soft:var(--warn-soft);--line:var(--warn-line)}
.banner.tone-ok{--tone:var(--ok);--soft:var(--ok-soft);--line:var(--ok-line)}
.banner.tone-warn{--tone:var(--warn);--soft:var(--warn-soft);--line:var(--warn-line)}
.banner.tone-err{--tone:var(--err);--soft:var(--err-soft);--line:var(--err-line)}
.banner.tone-mute{--tone:var(--text-3);--soft:var(--surface-2);--line:var(--border)}
.banner .note-warn{margin-top:10px}
.b-ico{width:40px;height:40px;border-radius:12px;display:grid;place-items:center;background:var(--tone);color:var(--on-tone);box-shadow:0 3px 10px color-mix(in srgb, var(--tone) 30%, transparent)}
.b-ico .i{width:22px;height:22px;stroke-width:2.1}
.banner.tone-idle .b-ico{background:var(--accent-soft);color:var(--accent);box-shadow:none}
.b-body{min-width:0}
.b-title{font-size:16px;line-height:1.5;font-weight:650;padding-top:1px}
.b-detail{font-size:14px;line-height:1.6;color:var(--text-2);margin-top:3px}
.b-acts{grid-column:2;display:flex;flex-wrap:wrap;gap:8px;margin-top:-2px}
.escal{font-size:12.5px;line-height:1.5;color:var(--text-3);margin-top:8px}
.fname{font-weight:700}
.progress{position:relative;height:6px;margin-top:14px;border-radius:99px;overflow:hidden;background:var(--accent-soft-2)}
.progress::after{content:"";position:absolute;top:0;bottom:0;left:0;width:38%;border-radius:inherit;background:var(--accent);animation:indet 1.5s ease-in-out infinite}
.pstrip{display:flex;align-items:flex-end;gap:6px;margin-top:12px;flex-wrap:wrap}
.pg{width:20px;height:27px;border-radius:3px;background:var(--paper);box-shadow:0 0 0 1px var(--accent-soft-2),0 1px 2px rgba(16,24,40,.1);
  background-image:linear-gradient(var(--paper-line),var(--paper-line)),linear-gradient(var(--paper-line),var(--paper-line)),linear-gradient(var(--paper-line),var(--paper-line));
  background-size:12px 2px,12px 2px,8px 2px;background-position:4px 6px,4px 11px,4px 16px;background-repeat:no-repeat;animation:pop .3s ease-out both}
.pg.next{background:transparent;box-shadow:none;border:1.5px dashed var(--accent);animation:blink 1.2s ease-in-out infinite}
/* numbered recovery steps */
.steps{list-style:none;padding:0;margin:10px 0 0;display:grid;gap:6px;counter-reset:st}
.steps li{position:relative;padding-left:30px;font-size:14px;line-height:1.55;color:var(--text-2);counter-increment:st}
.steps li::before{content:counter(st);position:absolute;left:0;top:1px;width:21px;height:21px;border-radius:50%;display:grid;place-items:center;
  background:var(--surface);border:1px solid var(--line);color:var(--tone);font-size:12px;font-weight:700}
/* print progress tracker: Sent — Receiving — Collect */
.track{display:flex;align-items:center;flex-wrap:wrap;gap:8px 6px;margin-top:12px;font-size:13.5px;color:var(--text-3)}
.track .s{display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.track .d{width:19px;height:19px;border-radius:50%;border:2px solid var(--border-strong);display:grid;place-items:center;flex:none;background:var(--surface)}
.track .d .i{width:12px;height:12px;stroke-width:3.4;color:var(--on-tone)}
.track .s.done{color:var(--text-2)}
.track .s.done .d{background:var(--ok);border-color:var(--ok)}
.track .s.doing{color:var(--accent-ink);font-weight:650}
.track .s.doing .d{border-color:var(--accent);border-right-color:transparent;background:transparent;animation:spin .9s linear infinite}
.track .s.here{color:var(--ok);font-weight:700}
.track .s.here .d{border-color:var(--ok);background:var(--surface);box-shadow:inset 0 0 0 4px var(--ok)}
.track .sep{flex:0 0 14px;height:2px;border-radius:2px;background:var(--border-strong)}
.track .sep.done{background:var(--ok)}

/* ---------- scan layout ---------- */
.panel-scan{display:grid;gap:16px;grid-template-areas:"status" "settings" "files"}
.panel-scan > #scan-status{grid-area:status}
.panel-scan > .scan-settings{grid-area:settings}
.panel-scan > .files-card{grid-area:files}
@media (min-width:960px){
  .panel-scan{grid-template-columns:minmax(0,7fr) minmax(0,5fr);grid-template-rows:auto 1fr;grid-template-areas:"settings status" "settings files";gap:24px;align-items:start}
}

/* scanned files */
.files{list-style:none;margin:0;padding:0}
.file{display:grid;grid-template-columns:40px minmax(0,1fr) auto;grid-template-areas:"ic name name" "ic meta acts";gap:0 12px;align-items:center;
  padding:10px 0 14px;border-top:1px solid var(--border);transition:opacity .25s,transform .25s}
.file > .ftype{grid-area:ic;align-self:start;margin-top:4px}
.file > .f-head{grid-area:name;display:flex;align-items:center;flex-wrap:wrap;gap:0 8px;min-width:0}
.file > .f-meta{grid-area:meta}
.file > .f-acts{grid-area:acts}
.file:first-child{border-top:0;padding-top:0}
.file:last-child{padding-bottom:0}
.file.is-new{border-top-color:transparent;margin:0 -12px;padding:10px 12px 14px;border-radius:14px;background:var(--ok-soft);box-shadow:inset 0 0 0 1px var(--ok-line)}
.file.is-new + .file{border-top-color:transparent}
.f-small{color:var(--accent,#2563eb);text-decoration:underline;text-underline-offset:2px}
.file.is-review{background:var(--warn-soft);box-shadow:inset 0 0 0 1px var(--warn-line);border-radius:14px;padding:10px 12px 14px;margin:0 -12px}
.file.is-review .badge-new{background:var(--warn)}
.file.is-leaving{opacity:0;transform:translateX(12px)}
.ftype{width:40px;height:46px;border-radius:8px;display:grid;place-items:end center;padding-bottom:6px;position:relative;flex:none;
  background:var(--err-soft);border:1px solid var(--err-line);color:var(--err);font-size:10.5px;font-weight:800;letter-spacing:.04em}
.ftype::before{content:"";position:absolute;top:8px;left:9px;right:9px;height:2px;border-radius:2px;background:currentColor;opacity:.35;box-shadow:0 5px 0 currentColor,0 10px 0 currentColor}
.ftype.doc{background:var(--accent-soft);border-color:var(--accent-soft-2);color:var(--accent-ink)}
.f-name{display:inline-flex;align-items:center;gap:6px;min-height:44px;max-width:100%;font-size:14.5px;font-weight:600;color:var(--text);text-decoration:none;line-height:1.4;border-radius:8px}
.f-name .f-text{min-width:0;overflow-wrap:normal;word-break:normal}
.f-name .i{width:16px;height:16px;color:var(--accent);flex:none}
.f-name:hover{color:var(--accent-ink)}
.f-name:hover .f-text{text-decoration:underline;text-underline-offset:3px}
.f-meta{display:flex;align-items:center;gap:2px 12px;flex-wrap:wrap;font-size:13px;color:var(--text-3)}
.f-meta .i{width:14px;height:14px}
.f-meta .soon{color:var(--warn);font-weight:600}
.f-meta .f-err{color:var(--err);font-weight:600}
.f-meta .f-review{flex-basis:100%;color:var(--warn);font-weight:600;line-height:1.5}
.f-meta .ttl{display:inline-flex;align-items:center;gap:5px;white-space:nowrap}
.badge-new{display:inline-flex;align-items:center;min-height:22px;padding:0 8px;border-radius:999px;background:var(--ok);color:var(--on-tone);font-size:12px;font-weight:700;white-space:nowrap}
.f-acts{display:flex;gap:8px}
@media (max-width:599px){
  .file{grid-template-columns:40px minmax(0,1fr);grid-template-areas:"ic name" "ic meta" ". acts"}
  .f-acts{margin-top:10px}
  .f-acts .btn{flex:1}
}
.empty{display:grid;justify-items:center;text-align:center;gap:6px;padding:22px 12px 14px;color:var(--text-3)}
.empty-ico{width:56px;height:56px;border-radius:18px;display:grid;place-items:center;background:var(--surface-2);border:1px solid var(--border);color:var(--text-3);margin-bottom:6px}
.empty-ico .i{width:26px;height:26px;stroke-width:1.6}
.empty b{font-size:15px;color:var(--text-2);font-weight:600}
.empty p{font-size:13px;max-width:280px}

/* ---------- print layout ---------- */
.panel-print > #print-status{margin-bottom:16px}
.print-grid{display:grid;gap:16px}
@media (min-width:960px){
  .print-grid{grid-template-columns:minmax(0,1fr) 384px;gap:24px;align-items:start}
  /* on desktop the print result sits inside the settings card, right above 打印 */
  .print-settings > #print-status{margin:0 20px 20px;box-shadow:none;border-radius:14px;padding:14px 16px;gap:12px}
  .print-settings > #print-status .b-ico{width:36px;height:36px;border-radius:11px}
  .print-settings > #print-status .b-title{font-size:15px}
}
@media (min-width:960px) and (min-height:800px){.print-settings{position:sticky;top:calc(var(--head-h) + 20px)}}

.drop{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;text-align:center;min-height:320px;padding:28px 20px;cursor:pointer;
  border:2px dashed var(--border-strong);border-radius:16px;background:var(--surface-2);transition:border-color .15s,background-color .15s}
.drop:hover,.drop.is-over,.doc-card.is-over .docbar{border-color:var(--accent);background:var(--accent-soft)}
#file-input:focus-visible + .drop{outline:3px solid var(--focus);outline-offset:3px}
.drop-ico{width:68px;height:68px;border-radius:20px;display:grid;place-items:center;background:var(--surface);color:var(--accent);box-shadow:var(--shadow);margin-bottom:6px}
.drop-ico .i{width:30px;height:30px;stroke-width:1.7}
.drop h3{font-size:17px;line-height:1.4;font-weight:650;color:var(--text)}
.drop .fmts{display:flex;flex-wrap:wrap;justify-content:center;gap:6px;margin:2px 0 4px}
.drop .fmts span{padding:2px 9px;border-radius:999px;background:var(--surface);border:1px solid var(--border);font-size:12.5px;color:var(--text-2)}
.drop .lim{font-size:12.5px;color:var(--text-3)}
.drop .btn{margin-top:8px;pointer-events:none}
.only-touch{display:none}
@media (min-width:960px){.drop{min-height:520px}}
@media (max-width:599px),(pointer:coarse){.drop{min-height:240px}.only-desk{display:none}.only-touch{display:inline}}
@media (max-width:599px){.drop .fmts{max-width:230px}}

.docbar{display:flex;align-items:center;gap:12px;padding:12px 12px 12px 14px;border:1px solid var(--border);border-radius:14px;background:var(--surface-2);transition:border-color .15s,background-color .15s}
.doc-info{flex:1;min-width:0}
.doc-line{font-size:15px;line-height:1.45}
.doc-line b{font-weight:650;overflow-wrap:break-word;word-wrap:break-word;overflow-wrap:anywhere}
.doc-line span{color:var(--text-2)}
.doc-sub{font-size:13px;color:var(--text-3);margin-top:2px}
.docbar .progress{margin-top:8px}

/* page selection box: sits directly above the thumbnails it controls */
.pagesel{margin-top:14px;padding:12px 16px 16px;border-radius:14px;background:var(--surface-2);border:1px solid var(--border)}
.pagesel .field-row{margin:0 -6px 4px 0}
.links{display:flex;flex:none}
.sumbox{display:flex;gap:10px;align-items:flex-start;margin-top:10px;padding:11px 13px;border-radius:12px;background:var(--accent-soft);border:1px solid var(--accent-soft-2);
  font-size:14px;line-height:1.55;color:var(--text-2)}
.sumbox > .i{width:17px;height:17px;margin-top:2px;color:var(--accent)}
.sumbox b{color:var(--text);font-weight:700}
.sumbox small{display:block;margin-top:2px;font-size:13px;font-weight:500;color:var(--text-2)}
.sumbox.is-muted{background:var(--surface);border-color:var(--border);color:var(--text-3)}
.sumbox.is-muted > .i{color:var(--text-3)}
.sumbox.is-err{background:var(--err-soft);border-color:var(--err-line);color:var(--err);font-weight:600}
.sumbox.is-err > .i{color:var(--err)}
.sumbox.is-warn{background:var(--warn-soft);border-color:var(--warn-line);color:var(--warn);font-weight:600}
.sumbox.is-warn > .i{color:var(--warn)}

.thumbs-head{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:4px 12px;margin:18px 0 12px;font-size:13px;color:var(--text-3)}
.thumbs-head .legend{display:inline-flex;align-items:center;gap:14px;flex-wrap:wrap}
.lg{display:inline-flex;align-items:center;gap:6px}
.lg i{width:11px;height:15px;border-radius:2px;background:var(--paper);box-shadow:0 0 0 1.5px var(--accent)}
.lg.off i{background:var(--paper-off);box-shadow:none;border:1.5px dashed var(--off-line)}
.thumbs{display:grid;grid-template-columns:repeat(auto-fill,minmax(114px,1fr));gap:18px 14px;padding:4px 4px 6px;margin:0 -4px}
@media (max-width:599px){.thumbs{grid-template-columns:repeat(3,minmax(0,1fr));gap:14px 10px}}
/* only long documents get their own scroll box (avoids a nested-scroll trap for short ones) */
.thumbs.capped{max-height:70vh;max-height:min(70vh,720px);overflow:auto;overscroll-behavior:contain;scrollbar-gutter:stable;margin:0;padding:12px 10px 16px;
  border-radius:14px;background:var(--surface-2);border:1px solid var(--border)}
.thumbs.capped.more{-webkit-mask-image:linear-gradient(180deg,#000 calc(100% - 48px),rgba(0,0,0,.25));mask-image:linear-gradient(180deg,#000 calc(100% - 48px),rgba(0,0,0,.25))}
.thumbs.is-stale .thumb,.thumbs.is-locked .thumb{pointer-events:none}
.thumbs.is-stale .sheet,.thumbs.is-stale .cap{opacity:.3;filter:grayscale(1)}
.thumb{display:grid;gap:8px;text-align:center;color:inherit;min-width:0;border-radius:8px}
.sheet{position:relative;aspect-ratio:210/297;transition:transform .15s}
.sheet::before{content:"";display:block;padding-top:141.4286%}   /* 297/210 — same box where aspect-ratio is missing */
/* tapping the page = look closer (lightbox); only the corner badge changes the selection */
.t-open{appearance:none;-webkit-appearance:none;border:0;background:none;padding:0;margin:0;position:absolute;top:0;right:0;bottom:0;left:0;display:block;width:100%;height:100%;
  cursor:zoom-in;border-radius:6px;color:inherit;font:inherit}
.t-open:focus-visible{outline:3px solid var(--focus);outline-offset:4px}
.zoom{position:absolute;right:6px;bottom:6px;display:inline-flex;align-items:center;gap:3px;height:22px;padding:0 7px 0 6px;border-radius:999px;
  background:var(--stamp-bg);color:var(--text-2);font-size:11px;font-weight:650;line-height:1;box-shadow:0 1px 3px rgba(16,24,40,.18);pointer-events:none}
.zoom .i{width:12px;height:12px;stroke-width:2.4}
.t-open:hover .zoom{color:var(--accent-ink)}
.paper{position:absolute;top:0;right:0;bottom:0;left:0;overflow:hidden;border-radius:6px;border:1.5px solid transparent;background:var(--paper);
  box-shadow:0 0 0 1px var(--border),0 1px 3px rgba(16,24,40,.08);display:flex;flex-direction:column;gap:5px;padding:14% 11% 0;
  transition:background-color .15s,box-shadow .15s,border-color .15s}
.paper.is-loading{background:linear-gradient(100deg,var(--surface-3) 30%,var(--surface-2) 50%,var(--surface-3) 70%);background-size:220% 100%;animation:shimmer 1.3s linear infinite}
/* real page preview (GET /preview/<token>?p=N): the PNG fills the sheet, letterboxed on white */
.paper > img{position:absolute;top:0;right:0;bottom:0;left:0;width:100%;height:100%;object-fit:contain;display:block;border-radius:inherit;transition:opacity .15s}
.paper.is-loading > img{opacity:0}
.pv-fail{position:absolute;top:0;right:0;bottom:0;left:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;padding:8px;
  font-size:12px;line-height:1.35;font-weight:600;color:var(--text-3);text-align:center}
.pv-fail .i{width:20px;height:20px;color:var(--text-3)}
/* the check badge is the toggle: 22 px dot, 32 px hit area (40 px on touch screens) centred on the same spot */
.tick{appearance:none;-webkit-appearance:none;border:0;background:none;padding:0;margin:0;position:absolute;top:1px;right:1px;width:32px;height:32px;display:grid;place-items:center;
  border-radius:50%;cursor:pointer;color:#fff}
.tick .dot{width:22px;height:22px;border-radius:50%;display:grid;place-items:center;background:var(--accent-btn);box-shadow:0 1px 3px rgba(16,24,40,.25);
  transition:background-color .15s,border-color .15s,box-shadow .15s}
.tick .i{width:13px;height:13px;stroke-width:3.2}
.tick:hover .dot{box-shadow:0 0 0 3px var(--accent-soft-2),0 1px 3px rgba(16,24,40,.25)}
.tick:focus-visible{outline:3px solid var(--focus);outline-offset:-2px}
@media (pointer:coarse){.tick{top:-3px;right:-3px;width:40px;height:40px}}
.stamp{display:none;position:absolute;left:50%;top:45%;transform:translate(-50%,-50%) rotate(-12deg);padding:2px 8px;border:2px solid var(--stamp-line);border-radius:6px;
  background:var(--stamp-bg);color:var(--stamp-ink);font-size:12.5px;font-weight:800;letter-spacing:.08em;white-space:nowrap}
.cap{font-size:12.5px;line-height:1.3;font-weight:650;color:var(--accent-ink)}
.thumb[data-on=true] .paper{box-shadow:0 0 0 2px var(--accent),0 6px 16px rgba(37,99,235,.16)}
.thumb[data-on=false] .paper{background:var(--paper-off);border-style:dashed;border-color:var(--off-line);box-shadow:none}
.thumb[data-on=false] .paper > *{opacity:var(--off-op)}
.thumb[data-on=false] .stamp{display:block}
.thumb[data-on=false] .tick .dot{background:var(--surface);border:1.5px solid var(--border-strong);box-shadow:none}
.thumb[data-on=false] .tick .i{opacity:0}
.thumb[data-on=false] .cap{color:var(--text-3);font-weight:500;text-decoration:line-through;text-decoration-color:var(--off-line)}
.thumb.is-skel{cursor:progress}
.thumb.is-skel .paper{box-shadow:0 0 0 1px var(--border)}
.thumb.is-skel .cap{color:var(--text-3);font-weight:500}
.thumb:not(.is-skel):hover .sheet{transform:translateY(-2px)}

/* ---------- page lightbox (tap a page to look closer; never changes the selection by itself) ---------- */
html.lb-open{overflow:hidden}
.lb:not([open]){display:none}
.lb.lb-fallback{z-index:100}
.lb-backdrop{position:fixed;top:0;right:0;bottom:0;left:0;z-index:99;background:rgba(9,11,16,.62)}
.lb{position:fixed;top:0;right:0;bottom:0;left:0;margin:auto;padding:0;border:0;width:92vw;max-width:980px;height:92vh;max-height:1120px;width:min(980px, calc(100vw - 48px));height:min(calc(100vh - 48px), 1120px);
  border-radius:20px;background:var(--surface);color:var(--text);box-shadow:var(--shadow-lg);overflow:hidden}
.lb[open]{display:flex;flex-direction:column}
.lb::backdrop{background:rgba(9,11,16,.62);-webkit-backdrop-filter:blur(4px);backdrop-filter:blur(4px)}
.lb-head{display:flex;align-items:center;gap:8px 12px;padding:12px 12px 12px 20px;border-bottom:1px solid var(--border);min-width:0}
.lb-head h2{font-size:17px;line-height:1.4;font-weight:650;white-space:nowrap}
.lb-head h2 span{margin-left:6px;font-size:14px;font-weight:500;color:var(--text-3)}
.lb-state{display:inline-flex;align-items:center;gap:5px;min-height:28px;padding:0 10px;border-radius:999px;font-size:13px;font-weight:650;white-space:nowrap}
.lb-state .i{width:14px;height:14px;stroke-width:2.6}
.lb-state.is-on{background:var(--accent-soft);color:var(--accent-ink);box-shadow:inset 0 0 0 1px var(--accent-soft-2)}
.lb-state.is-off{background:var(--surface-3);color:var(--text-2);box-shadow:inset 0 0 0 1.5px var(--off-line)}
.lb-x{margin-left:auto;flex:none;appearance:none;-webkit-appearance:none;border:0;width:44px;height:44px;border-radius:12px;display:grid;place-items:center;cursor:pointer;
  background:var(--surface-2);color:var(--text)}
.lb-x:hover{background:var(--surface-3)}
.lb-body{position:relative;flex:1;min-height:0;padding:16px;background:var(--surface-2);touch-action:pan-y pinch-zoom}
.lb-page{position:relative;width:100%;height:100%}
.lb-page img{position:absolute;top:0;right:0;bottom:0;left:0;width:100%;height:100%;object-fit:contain;opacity:0;transition:opacity .15s;
  filter:drop-shadow(0 2px 10px rgba(16,24,40,.2))}
.lb-page.lo-ready .lb-lo,.lb-page.hi-ready .lb-hi{opacity:1}
.lb-page.is-off.lo-ready .lb-lo,.lb-page.is-off.hi-ready .lb-hi{opacity:.55}
.lb-page.is-loading::after{content:"";position:absolute;top:0;right:0;bottom:0;left:0;margin:auto;width:30px;height:30px;border-radius:50%;
  border:3px solid var(--accent);border-right-color:transparent;animation:spin .8s linear infinite}
.lb-note,.lb-fail{display:none;position:absolute;left:50%;transform:translateX(-50%);margin:0;padding:6px 12px;border-radius:999px;font-size:13px;font-weight:600;
  white-space:nowrap;background:var(--surface);color:var(--text-2);box-shadow:var(--shadow)}
.lb-note{bottom:8px}
.lb-fail{top:50%;transform:translate(-50%,-50%);display:none;align-items:center;gap:6px;white-space:normal;text-align:center;border-radius:14px;padding:12px 16px}
.lb-fail .i{width:18px;height:18px;color:var(--text-3)}
.lb-page.lo-only .lb-note{display:block}
.lb-page.failed .lb-fail{display:flex}
.lb-foot{display:flex;align-items:center;gap:10px;padding:12px 16px;padding:12px 16px calc(12px + env(safe-area-inset-bottom));border-top:1px solid var(--border);background:var(--surface)}
.lb-foot .btn{min-height:48px}
.lb-foot .lb-toggle{flex:1;min-width:0}
.lb-foot .lb-toggle span{overflow:hidden;text-overflow:ellipsis}
.i.rot-l{transform:rotate(90deg)} .i.rot-r{transform:rotate(-90deg)}
@media (min-width:600px){.lb-foot .lb-toggle{flex:0 1 240px;margin:0 auto}}
@media (max-width:599px){
  .lb{width:100vw;height:100vh;height:100dvh;max-width:none;max-height:none;border-radius:0}
  .lb-head{padding:8px 8px 8px 16px}
  .lb-body{padding:10px}
  .lb-foot{gap:8px;padding:10px 12px;padding:10px 12px calc(10px + env(safe-area-inset-bottom))}
  .lb-foot .lb-nav{padding:0 12px}
}

/* print settings */
.copies{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.copies .field-label{margin:0}
.stepper{display:inline-grid;grid-template-columns:48px 72px 48px;height:48px;border:1px solid var(--border-strong);border-radius:12px;overflow:hidden;background:var(--field)}
.stepper button{appearance:none;-webkit-appearance:none;border:0;background:var(--surface-2);cursor:pointer;display:grid;place-items:center;color:var(--text)}
.stepper button:hover:not(:disabled){background:var(--surface-3)}
.stepper button:disabled{color:var(--text-3);cursor:not-allowed;opacity:.6}
.stepper button:focus-visible{outline-offset:-3px}
.stepper input{width:100%;border:0;border-left:1px solid var(--border);border-right:1px solid var(--border);background:transparent;text-align:center;font-size:17px;font-weight:650;font-variant-numeric:tabular-nums;-moz-appearance:textfield;appearance:textfield}
.stepper input::-webkit-inner-spin-button,.stepper input::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}
.stepper input:focus-visible{outline:none;box-shadow:inset 0 0 0 2px var(--accent)}
.fine{display:flex;gap:8px;align-items:flex-start;margin-top:22px;padding-top:16px;border-top:1px dashed var(--border);font-size:13px;line-height:1.55;color:var(--text-3)}
.fine .i{width:16px;height:16px;margin-top:2px}

/* ---------- browsers without flex `gap` (Safari < 14.1, Chrome < 84; html.no-flexgap is set by a feature test):
   the same spacing as margins. Icons sit next to bare text, so the icon carries the margin. ---------- */
.no-flexgap .btn > .i,.no-flexgap .btn > .spin{margin-right:8px}
.no-flexgap .btn-sm > .i,.no-flexgap .btn-sm > .spin{margin-right:6px}
.no-flexgap #lb-next > .i{margin:0 0 0 8px}
.no-flexgap .chip > .dot{margin-right:8px}
.no-flexgap .chip > b{margin-left:8px}
.no-flexgap .meta-note > .i,.no-flexgap .tag > .i,.no-flexgap .lg > i,.no-flexgap .track .s > .d,.no-flexgap .lb-fail > .i{margin-right:6px}
.no-flexgap .f-meta .ttl > .i,.no-flexgap .lb-state > .i{margin-right:5px}
.no-flexgap .zoom > .i{margin-right:3px}
.no-flexgap .note-warn > .i,.no-flexgap .fine > .i{margin-right:8px}
.no-flexgap .tip > .i,.no-flexgap .sumbox > .i{margin-right:10px}
.no-flexgap .card-h h2 > .count,.no-flexgap .field-label > .unit{margin-left:8px}
.no-flexgap .check > .check-t{margin-left:12px}
.no-flexgap .f-name > .i{margin-left:6px}
.no-flexgap .pstrip > * + *,.no-flexgap .track > * + *{margin-left:6px}
.no-flexgap .f-acts > * + *,.no-flexgap .status-area > * + *,.no-flexgap .field-row > * + *,.no-flexgap .file > .f-head > * + *,
.no-flexgap .lb-head > * + *{margin-left:8px}
.no-flexgap .tab > * + *,.no-flexgap .bar-acts > :not([hidden]) ~ :not([hidden]),.no-flexgap .lb-foot > * + *{margin-left:10px}
.no-flexgap .brand > * + *,.no-flexgap .docbar > * + *,.no-flexgap .card-h > * + *,.no-flexgap .f-meta > * + *,
.no-flexgap .thumbs-head > * + *,.no-flexgap .copies > * + *{margin-left:12px}
.no-flexgap .legend > * + *{margin-left:14px}
.no-flexgap .actionbar > * + *{margin-left:16px}
.no-flexgap .b-acts > *,.no-flexgap .drop .fmts > *{margin:0 8px 8px 0}
.no-flexgap .b-acts{margin-bottom:-8px}
.no-flexgap .tile .t > b,.no-flexgap .tile .t > small{margin-top:4px}
.no-flexgap .drop > * + *{margin-top:10px}
.no-flexgap .pv-fail > .i{margin-bottom:6px}

/* motion */
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes indet{0%{transform:translateX(-100%)}100%{transform:translateX(270%)}}
@keyframes shimmer{0%{background-position:120% 0}100%{background-position:-120% 0}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
@keyframes blink{0%,100%{opacity:.35}50%{opacity:1}}
@keyframes pop{from{transform:translateY(4px) scale(.9);opacity:0}to{transform:none;opacity:1}}
@keyframes rise{from{transform:translateY(4px);opacity:0}to{transform:none;opacity:1}}
@media (prefers-reduced-motion: reduce){
  *,*::before,*::after{animation-duration:.01ms !important;animation-iteration-count:1 !important;transition-duration:.01ms !important}
  .progress::after{width:100%;opacity:.55}
}
</style>
</head>
<body>

<!-- inline icon sprite (no external resources) -->
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <symbol id="i-printer" viewBox="0 0 24 24"><path d="M7 9V4h10v5"/><path d="M7 17H5a2 2 0 0 1-2-2v-4a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2h-2"/><path d="M7 14h10v6H7z"/><path d="M17.5 11.5h.01"/></symbol>
  <symbol id="i-scan" viewBox="0 0 24 24"><path d="M3 14.5h18v4a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5z"/><path d="M4.2 14.5 7.6 6H20"/><path d="M6.5 17.5h4"/></symbol>
  <symbol id="i-flatbed" viewBox="0 0 24 24"><path d="M3 14h18v4.5A1.5 1.5 0 0 1 19.5 20h-15A1.5 1.5 0 0 1 3 18.5z"/><path d="M4 14 7.5 4.5h13"/><path d="M8 12.5h8"/><path d="M6.5 17h3"/></symbol>
  <symbol id="i-feeder" viewBox="0 0 24 24"><path d="M3 14h18v4.5A1.5 1.5 0 0 1 19.5 20h-15A1.5 1.5 0 0 1 3 18.5z"/><path d="M7.5 14V3.5h9V14"/><path d="M10 7h4M10 10h4"/><path d="M6.5 17h3"/></symbol>
  <symbol id="i-feeder2" viewBox="0 0 24 24"><path d="M3 14h18v4.5A1.5 1.5 0 0 1 19.5 20h-15A1.5 1.5 0 0 1 3 18.5z"/><path d="M9.5 14V3.5h9V14"/><path d="M5.5 14V6.5h4"/><path d="M12 7h4M12 10h4"/><path d="M6.5 17h3"/></symbol>
  <symbol id="i-simplex" viewBox="0 0 24 24"><rect x="6" y="3" width="12" height="18" rx="1.5"/><path d="M9 8h6M9 11h6M9 14h4"/></symbol>
  <symbol id="i-book" viewBox="0 0 24 24"><path d="M12 7v13"/><path d="M12 7C10.4 5.6 7.6 5 3.5 5v13c4.1 0 6.9.6 8.5 2"/><path d="M12 7c1.6-1.4 4.4-2 8.5-2v13c-4.1 0-6.9.6-8.5 2"/></symbol>
  <symbol id="i-pad" viewBox="0 0 24 24"><rect x="4.5" y="6" width="15" height="15" rx="1.5"/><path d="M8.5 3.5v4.5M12 3.5v4.5M15.5 3.5v4.5"/><path d="M8 12.5h8M8 16h5"/></symbol>
  <symbol id="i-check" viewBox="0 0 24 24"><path d="M5 12.5 10 17.5 19 7"/></symbol>
  <symbol id="i-alert" viewBox="0 0 24 24"><path d="M12 9v4.5"/><path d="M12 17v.01"/><path d="M10.3 4.2 2.6 17.5A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0z"/></symbol>
  <symbol id="i-alert-circle" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7.5V13"/><path d="M12 16.5v.01"/></symbol>
  <symbol id="i-info" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v5.5"/><path d="M12 7.8v.01"/></symbol>
  <symbol id="i-hourglass" viewBox="0 0 24 24"><path d="M6.5 3h11M6.5 21h11"/><path d="M8 3v2.8a4 4 0 0 0 1.8 3.3L12 10.6l2.2-1.5A4 4 0 0 0 16 5.8V3"/><path d="M8 21v-2.8a4 4 0 0 1 1.8-3.3L12 13.4l2.2 1.5a4 4 0 0 1 1.8 3.3V21"/></symbol>
  <symbol id="i-download" viewBox="0 0 24 24"><path d="M12 4v11"/><path d="m7 10 5 5 5-5"/><path d="M5 20h14"/></symbol>
  <symbol id="i-upload" viewBox="0 0 24 24"><path d="M12 15V4"/><path d="m7 9 5-5 5 5"/><path d="M5 20h14"/></symbol>
  <symbol id="i-trash" viewBox="0 0 24 24"><path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="m6 7 1 12.5A1.5 1.5 0 0 0 8.5 21h7a1.5 1.5 0 0 0 1.5-1.5L18 7"/><path d="M9 7V4.5A1.5 1.5 0 0 1 10.5 3h3A1.5 1.5 0 0 1 15 4.5V7"/></symbol>
  <symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></symbol>
  <symbol id="i-retry" viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.35-5.65"/><path d="M20 4v5h-5"/></symbol>
  <symbol id="i-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></symbol>
  <symbol id="i-minus" viewBox="0 0 24 24"><path d="M5 12h14"/></symbol>
  <symbol id="i-chevron" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></symbol>
  <symbol id="i-file" viewBox="0 0 24 24"><path d="M14 3H7.5A1.5 1.5 0 0 0 6 4.5v15A1.5 1.5 0 0 0 7.5 21h9a1.5 1.5 0 0 0 1.5-1.5V7z"/><path d="M14 3v4h4"/><path d="M9 12h6M9 15.5h6"/></symbol>
  <symbol id="i-pages" viewBox="0 0 24 24"><rect x="4" y="6" width="11" height="15" rx="1.5"/><path d="M8 3h10.5A1.5 1.5 0 0 1 20 4.5V17"/></symbol>
  <symbol id="i-bw" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 3.5a8.5 8.5 0 0 1 0 17z" fill="currentColor"/></symbol>
  <symbol id="i-lock" viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="9.5" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></symbol>
  <symbol id="i-x" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></symbol>
  <symbol id="i-zoom" viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m15.5 15.5 5 5"/><path d="M10.5 8v5M8 10.5h5"/></symbol>
</svg>

<header class="top">
  <div class="top-in">
    <div class="brand">
      <span class="mark" aria-hidden="true"><svg class="i"><use href="#i-printer"/></svg></span>
      <div class="brand-t">
        <h1>Office Scan & Print Station</h1>
        <p class="brand-sub">HP LaserJet MFP M437nda · Scan paper to PDF or upload a file to print</p>
      </div>
    </div>

    <div class="tabs" role="tablist" aria-label="Choose a function">
      <button class="tab" role="tab" id="tab-scan" data-tab="scan" aria-controls="panel-scan" aria-selected="true">
        <svg class="i" aria-hidden="true"><use href="#i-scan"/></svg><span class="tab-t"><b>Scan</b><small>Paper to PDF</small></span></button>
      <button class="tab" role="tab" id="tab-print" data-tab="print" aria-controls="panel-print" aria-selected="false" tabindex="-1">
        <svg class="i" aria-hidden="true"><use href="#i-printer"/></svg><span class="tab-t"><b>Print</b><small>Print a file</small></span></button>
    </div>

    <div class="status-area" role="status" aria-live="polite" aria-label="Device status">
      <span class="chip" id="chip-scanner" data-tone="idle"><i class="dot" aria-hidden="true"></i>Scanner <b class="chip-v">Checking...</b></span>
      <span class="chip" id="chip-printer" data-tone="idle"><i class="dot" aria-hidden="true"></i>Printer <b class="chip-v">Checking...</b></span>
    </div>
  </div>
</header>

<main class="wrap">
  <!-- ===================== 扫描 ===================== -->
  <section class="panel-scan" id="panel-scan" role="tabpanel" aria-labelledby="tab-scan">

    <div id="scan-status" class="banner tone-idle" role="status" aria-live="polite"></div>

    <form class="card scan-settings" id="scan-form" novalidate aria-labelledby="scan-title">
      <div class="card-h">
        <h2 id="scan-title">Scan settings</h2>
        <span class="meta-note" id="scan-meta">Choose the source; the other defaults usually work</span>
        <span class="meta-note is-lock" id="scan-lock" hidden><svg class="i" aria-hidden="true"><use href="#i-lock"/></svg>Settings are locked during scanning</span>
      </div>
      <fieldset class="card-b" id="scan-fields">
        <legend class="sr">Scan settings</legend>
        <div class="group">
          <fieldset class="field">
            <legend class="field-label">Scan source</legend>
            <div class="tiles" id="source-tiles"></div>
            <div class="tip">
              <svg class="i" aria-hidden="true"><use href="#i-info"/></svg>
              <div>
                <p><b>Flatbed</b>: For books, IDs, receipts, thin or creased paper. Place one page face down at the top-left corner of the glass. Full area scans the entire glass; a selected size scans that area from the top-left corner.</p>
                <p><b>Feeder</b>: For a stack of flat sheets. Insert face up, short edge first, with the guides snug, until the printer detects the stack. Selected sizes include about 5 mm extra on each side. A3 fills the feeder width, so several millimeters on each long edge remain unseen. The leading edge also has an unseen strip of about 2 mm. Choose Full area if the size is unknown.</p>
              </div>
            </div>
          </fieldset>
        </div>

        <div class="group">
          <div class="row2">
            <div class="field">
              <label class="field-label" for="scan-size">Paper size</label>
              <div class="select">
                <select id="scan-size" name="size" aria-describedby="size-hint"></select>
                <svg class="i" aria-hidden="true"><use href="#i-chevron"/></svg>
              </div>
              <p class="hint" id="size-hint">Use the default if unsure; the scan area is kept uncropped.</p>
              <p class="note-warn" id="size-warn" hidden><svg class="i" aria-hidden="true"><use href="#i-alert"/></svg><span id="size-warn-t"></span></p>
            </div>
            <fieldset class="field">
              <legend class="field-label">Color</legend>
              <div class="seg" id="mode-seg"></div>
              <p class="hint">Use grayscale for monochrome documents and smaller files.</p>
            </fieldset>
          </div>
          <fieldset class="field">
            <legend class="field-label">Resolution <span class="unit">dpi; higher resolution creates larger files</span></legend>
            <div class="seg" id="res-seg"></div>
            <p class="hint" id="res-hint"></p>
          </fieldset>
          <fieldset class="field">
            <legend class="field-label">Paper background</legend>
            <label class="check"><input type="checkbox" name="paper_white" value="1" checked><span class="box"><svg class="i"><use href="#i-check"/></svg></span>
              <span class="check-t"><b>Whiten paper</b><small>Whiten plain paper while preserving text, seals and signatures. Colored paper is retained. Original scans remain available for download.</small></span></label>
          </fieldset>
        </div>

      </fieldset>

      <div class="actionbar" id="scan-bar">
        <div class="sum" aria-live="polite">
          <p class="sum-main" id="scan-sum-main"></p>
          <p class="sum-sub" id="scan-sum-sub"></p>
        </div>
        <div class="bar-acts">
          <a class="btn btn-primary btn-lg" id="scan-dl" href="#" download hidden><svg class="i" aria-hidden="true"><use href="#i-download"/></svg>Download PDF</a>
          <button type="submit" class="btn btn-primary btn-lg" id="scan-go">
            <svg class="i ico-idle" aria-hidden="true"><use href="#i-scan"/></svg><span class="spin" hidden></span><span class="lbl">Start scan</span>
          </button>
        </div>
      </div>
    </form>

    <section class="card files-card" aria-labelledby="files-title">
      <div class="card-h">
        <h2 id="files-title">Scanned PDFs <span class="count" id="files-count">0</span></h2>
        <span class="meta-note"><svg class="i" aria-hidden="true"><use href="#i-clock"/></svg><span class="retain-min">15</span> minutes before automatic deletion</span>
      </div>
      <div class="card-b">
        <ul class="files" id="files"></ul>
        <div class="empty" id="files-empty" hidden>
          <span class="empty-ico"><svg class="i" aria-hidden="true"><use href="#i-file"/></svg></span>
          <b>No scans yet</b>
          <p>Scanned PDFs appear here for download or printing. <span class="retain-min">15</span> minutes before automatic deletion.</p>
        </div>
      </div>
    </section>
  </section>

  <!-- ===================== 打印 ===================== -->
  <section class="panel-print" id="panel-print" role="tabpanel" aria-labelledby="tab-print" hidden>
    <!-- #print-status is moved by JS: top of this panel on phones, inside the settings card (above 打印) on desktop -->
    <div id="print-status" class="banner" role="status" aria-live="polite" hidden></div>

    <div class="print-grid" id="print-grid">
      <section class="card doc-card" id="doc-card" aria-labelledby="doc-title">
        <div class="card-h">
          <h2 id="doc-title">Choose a file to print</h2>
          <span class="meta-note" id="doc-hint"></span>
        </div>
        <div class="card-b">
          <input type="file" id="file-input" class="sr" aria-label="Choose a file to print"
                 accept=".pdf,.png,.jpg,.jpeg,.bmp,.gif,.tif,.tiff,.webp,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.odt,.ods,.odp,.rtf,.txt,.csv,.wps,.et,.dps">
          <label class="drop" id="drop" for="file-input">
            <span class="drop-ico"><svg class="i" aria-hidden="true"><use href="#i-upload"/></svg></span>
            <h3><span class="only-desk">Drop a file here or click to choose</span><span class="only-touch">Tap to choose a file</span></h3>
            <span class="fmts"><span>PDF</span><span>Images</span><span>Word</span><span>Excel</span><span>PPT</span><span>WPS</span></span>
            <span class="lim">Maximum file size <span id="max-mb">120</span> MB</span>
            <span class="btn btn-secondary"><svg class="i" aria-hidden="true"><use href="#i-file"/></svg>Choose file</span>
          </label>

          <div class="docbar" id="docbar" hidden>
            <span class="ftype" id="doc-type">PDF</span>
            <div class="doc-info">
              <p class="doc-line"><b id="doc-name"></b><span id="doc-meta"></span></p>
              <div class="progress" id="doc-progress" hidden></div>
              <p class="doc-sub" id="doc-sub" hidden></p>
            </div>
            <button type="button" class="btn btn-secondary btn-sm" id="doc-change"><svg class="i" aria-hidden="true"><use href="#i-upload"/></svg>Change file</button>
          </div>

          <!-- page selection: right above the thumbnails it is synced with -->
          <div class="pagesel" id="pagesel" hidden>
            <div class="field-row">
              <label class="field-label" for="pages">Pages to print</label>
              <span class="links"><button type="button" class="link" id="sel-all">Select all</button><button type="button" class="link" id="sel-none">Clear selection</button></span>
            </div>
            <input class="input" id="pages" name="pages" form="print-form" type="text" inputmode="text" autocomplete="off" spellcheck="false"
                   placeholder="Blank = all pages; e.g. 1-3,5,8-10" aria-describedby="pages-sum">
            <div class="sumbox" id="pages-sum" aria-live="polite"></div>
          </div>

          <div class="thumbs-head" id="thumbs-head" hidden>
            <span>Use each corner checkmark to select pages; tap a page to enlarge it</span>
            <span class="legend" aria-hidden="true"><span class="lg"><i></i>Print</span><span class="lg off"><i></i>Excluded</span></span>
          </div>
          <div class="thumbs" id="thumbs" role="group" aria-label="Page previews: tap a page to enlarge it; use its corner checkmark to select it" hidden></div>
        </div>
      </section>

      <form class="card print-settings" id="print-form" novalidate aria-labelledby="ps-title">
        <input type="hidden" name="token" id="print-token" value="">
        <div class="card-h">
          <h2 id="ps-title">Print settings</h2>
          <span class="tag"><svg class="i" aria-hidden="true"><use href="#i-bw"/></svg>Monochrome printer</span>
        </div>
        <div class="card-b">
          <p class="note-warn" id="doc-quality" hidden><svg class="i" aria-hidden="true"><use href="#i-alert"/></svg><span></span></p>
          <div class="field">
            <label class="field-label" for="paper">Paper</label>
            <div class="select">
              <select id="paper" name="paper"></select>
              <svg class="i" aria-hidden="true"><use href="#i-chevron"/></svg>
            </div>
            <p class="note-warn" id="paper-hint" hidden><svg class="i" aria-hidden="true"><use href="#i-alert"/></svg><span></span></p>
            <p class="note-warn is-info" id="paper-note" hidden><svg class="i" aria-hidden="true"><use href="#i-info"/></svg><span></span></p>
          </div>

          <fieldset class="field">
            <legend class="field-label">Scaling</legend>
            <div class="seg two-line" id="scale-seg"></div>
            <p class="hint" id="scale-hint"></p>
          </fieldset>

          <fieldset class="field">
            <legend class="field-label">Sides</legend>
            <div class="tiles compact" id="sides-tiles"></div>
            <p class="hint">Choose long-edge binding to turn pages like a book. Choose short-edge binding to flip pages like a calendar.</p>
          </fieldset>

          <div class="field">
            <div class="copies">
              <label class="field-label" for="copies">Copies</label>
              <div class="stepper">
                <button type="button" id="copies-minus" aria-label="One fewer copy"><svg class="i" aria-hidden="true"><use href="#i-minus"/></svg></button>
                <input id="copies" name="copies" type="number" inputmode="numeric" min="1" max="200" step="1" value="1">
                <button type="button" id="copies-plus" aria-label="One more copy"><svg class="i" aria-hidden="true"><use href="#i-plus"/></svg></button>
              </div>
            </div>
            <p class="hint">1–200 copies. Multiple copies are collated into complete sets.</p>
          </div>

          <p class="fine"><svg class="i" aria-hidden="true"><use href="#i-bw"/></svg>This is a monochrome printer. Color content prints in grayscale.</p>
        </div>

        <div class="actionbar" id="print-bar">
          <div class="sum" aria-live="polite">
            <p class="sum-main" id="print-sum-main"></p>
            <p class="sum-sub" id="print-sum-sub"></p>
          </div>
          <div class="bar-acts">
            <button type="button" class="btn btn-primary btn-lg" id="print-go" disabled>
              <svg class="i ico-idle" aria-hidden="true"><use href="#i-printer"/></svg><span class="spin" hidden></span><span class="lbl">Print</span>
            </button>
          </div>
        </div>
      </form>
    </div>
  </section>
</main>

<!-- page lightbox: opened by tapping a page preview -->
<div class="lb-backdrop" id="lb-backdrop" hidden></div>
<dialog class="lb" id="lightbox" aria-labelledby="lb-title" aria-modal="true">
  <div class="lb-head">
    <h2 id="lb-title">Page 1</h2>
    <span class="lb-state is-on" id="lb-state"></span>
    <button type="button" class="lb-x" id="lb-close" aria-label="Close enlarged preview (Esc)" autofocus><svg class="i" aria-hidden="true"><use href="#i-x"/></svg></button>
  </div>
  <div class="lb-body" id="lb-body">
    <div class="lb-page is-loading" id="lb-page">
      <img class="lb-lo" id="lb-lo" alt="" aria-hidden="true">
      <img class="lb-hi" id="lb-hi" alt="">
      <p class="lb-note">The full-size preview is unavailable; showing the thumbnail</p>
      <p class="lb-fail"><svg class="i" aria-hidden="true"><use href="#i-alert-circle"/></svg>Preview failed. This page can still be selected and printed</p>
    </div>
  </div>
  <div class="lb-foot">
    <button type="button" class="btn btn-secondary lb-nav" id="lb-prev"><svg class="i rot-l" aria-hidden="true"><use href="#i-chevron"/></svg>Previous page</button>
    <button type="button" class="btn lb-toggle" id="lb-toggle"></button>
    <button type="button" class="btn btn-secondary lb-nav" id="lb-next">Next page<svg class="i rot-r" aria-hidden="true"><use href="#i-chevron"/></svg></button>
  </div>
</dialog>


<script>
(() => {
'use strict';
/* boot data injected by the server: option lists, current files, limits */
const BOOT = __BOOT_JSON__;

/* old browsers (Safari < 14.1, Chrome < 84) ignore `gap` on flex boxes: html.no-flexgap switches the CSS to margins */
(function () {
  try {
    const d = document.createElement('div');
    d.style.cssText = 'display:flex;flex-direction:column;row-gap:1px;position:absolute;visibility:hidden';
    d.appendChild(document.createElement('div')); d.appendChild(document.createElement('div'));
    document.body.appendChild(d);
    const okGap = d.scrollHeight === 1;
    document.body.removeChild(d);
    if (!okGap) document.documentElement.className += ' no-flexgap';
  } catch (e) { /* keep the modern layout */ }
})();
const NATIVE_LAZY = 'loading' in HTMLImageElement.prototype;
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
/* file names may only break after "-" (never "…101500-3 / p.pdf") */
const fileHTML = s => esc(s).replace(/-/g, '-<wbr>');
const icon = id => `<svg class="i" aria-hidden="true"><use href="#i-${id}"/></svg>`;
const fileURL = name => '/f/' + encodeURIComponent(name);
const deskMQ = matchMedia('(min-width: 960px)');
const isPhone = () => !deskMQ.matches;
const NET_ERR = 'Cannot reach the station. Check the network and refresh the page';
const opts = key => (Array.isArray(BOOT[key]) ? BOOT[key] : []).map(o => Array.isArray(o) ? [String(o[0]), String(o[1] != null ? o[1] : o[0])] : [String(o), String(o)]);
const labelOf = (key, v) => (opts(key).find(o => o[0] === v) || [v, v])[1];
/* "Flatbed (place face down on the glass)" → ["Flatbed", "掀盖放玻璃上"]; "Double-sided, long edge (LEF)" → ["双面·长边翻", "LEF"] */
const splitLabel = l => { const m = /^(.*?)\s*[（(]([^（）()]+)[）)]\s*$/.exec(l); return m ? [m[1], m[2]] : [l, '']; };
/* server messages that already start with "Error: " must not get a second prefix */
const stripTrouble = m => String(m || '').replace(/^Error[：:]\s*/, '');
/* 4xx = the request itself can't work (bad file, expired…): the message already says what to do */
const needsAdmin = r => !(r.status >= 400 && r.status < 500);

/* ---------- tiny persistence (per tab): my job ids + the open tab ---------- */
const store = {
  get(k) { try { return JSON.parse(sessionStorage.getItem('station.' + k)); } catch (e) { return null; } },
  set(k, v) { try { if (v == null) sessionStorage.removeItem('station.' + k); else sessionStorage.setItem('station.' + k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
};

/* ---------- fetch wrapper: server JSON errors are shown verbatim; only a rejected fetch means "can't reach" ---------- */
async function api(url, {method = 'GET', body} = {}) {
  let r;
  try {
    r = await fetch(url, {method, body, cache: 'no-store', credentials: 'same-origin', headers: {Accept: 'application/json'}});
  } catch (e) {
    return {ok: false, status: 0, net: true, error: NET_ERR};
  }
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (r.ok && data && typeof data === 'object' && !data.error) return {ok: true, status: r.status, data};
  let error = data && typeof data.error === 'string' && data.error.trim() ? data.error : '';
  if (!error) error = r.status === 401 ? 'Your login expired. Refresh the page to sign in again'
    : `The station returned an unexpected response (HTTP ${r.status}). Retry or contact the administrator`;
  return {ok: false, status: r.status, error, data};
}

/* ================= tabs ================= */
const tabs = $$('[role=tab]');
function setTab(name, {focus = false} = {}) {
  if (name !== 'scan' && name !== 'print') name = 'scan';
  tabs.forEach(t => {
    const on = t.dataset.tab === name;
    t.setAttribute('aria-selected', on);
    t.tabIndex = on ? 0 : -1;
    if (on && focus) t.focus();
  });
  $('#panel-scan').hidden = name !== 'scan';
  $('#panel-print').hidden = name !== 'print';
  store.set('tab', name);
  if (name === 'print') requestAnimationFrame(updateMore);
}
tabs.forEach((t, i) => {
  t.addEventListener('click', () => setTab(t.dataset.tab));
  t.addEventListener('keydown', e => {
    const k = e.key; let j = null;
    if (k === 'ArrowRight') j = (i + 1) % tabs.length;
    else if (k === 'ArrowLeft') j = (i - 1 + tabs.length) % tabs.length;
    else if (k === 'Home') j = 0; else if (k === 'End') j = tabs.length - 1;
    if (j !== null) { e.preventDefault(); setTab(tabs[j].dataset.tab, {focus: true}); }
  });
});

/* ================= header chips =================
   tone: ok | busy | wait | err | idle(grey)  — driven only by GET /status */
function setDeviceChip(which, tone, value, title = '') {
  const c = $('#chip-' + which);
  if (c.dataset.tone !== tone) c.dataset.tone = tone;
  const v = c.querySelector('.chip-v');
  if (v.textContent !== value) v.textContent = value;
  if (title) c.title = title; else c.removeAttribute('title');
}

/* ================= banner renderer =================
   cfg = {tone, icon|spinner, title(html), detail(html), extra(html), steps[], escalate, progress, actions[]} */
function renderBanner(el, cfg) {
  if (!cfg) { el.hidden = true; el.innerHTML = ''; el._sig = ''; return; }
  const acts = (cfg.actions || []).map(a => a.href
    ? `<a class="btn btn-sm btn-${a.kind || 'secondary'}" href="${esc(a.href)}"${a.download ? ` download="${esc(a.download)}"` : ''}${a.newTab ? ' target="_blank" rel="noopener"' : ''}>${a.icon ? icon(a.icon) : ''}${esc(a.label)}</a>`
    : `<button type="button" class="btn btn-sm btn-${a.kind || 'secondary'}" data-act="${esc(a.act)}"${a.file ? ` data-file="${esc(a.file)}"` : ''}${a.disabled ? ' disabled' : ''}>${a.icon ? icon(a.icon) : ''}${esc(a.label)}</button>`).join('');
  const html = `
    <div class="b-ico" aria-hidden="true">${cfg.spinner ? '<span class="spin" style="width:20px;height:20px"></span>' : icon(cfg.icon)}</div>
    <div class="b-body">
      <p class="b-title">${cfg.title}</p>
      ${cfg.detail ? `<p class="b-detail">${cfg.detail}</p>` : ''}
      ${cfg.extra || ''}
      ${cfg.steps ? `<ol class="steps">${cfg.steps.map(s => `<li>${esc(s)}</li>`).join('')}</ol>` : ''}
      ${cfg.progress ? '<div class="progress" role="progressbar" aria-label="In progress"></div>' : ''}
      ${cfg.escalate ? '<p class="escal">If this persists, send this message to the administrator.</p>' : ''}
    </div>
    ${acts ? `<div class="b-acts">${acts}</div>` : ''}`;
  const cls = 'banner tone-' + cfg.tone;
  el.hidden = false;
  if (el._sig === cls + html) return;           // polled every few seconds: don't restart animations for nothing
  el._sig = cls + html;
  el.className = cls;
  el.innerHTML = html;
}
function setBar(which, {main, sub, tone} = {}) {
  const m = $(`#${which}-sum-main`), s = $(`#${which}-sum-sub`);
  if (main != null && m.textContent !== main) m.textContent = main;
  if (sub != null && s.textContent !== sub) s.textContent = sub;
  if (tone) s.dataset.tone = tone; else delete s.dataset.tone;
}
function setButton(btn, {disabled, busy = false, label, kind = 'primary', ico}) {
  btn.disabled = !!disabled;
  btn.classList.toggle('is-busy', busy);
  btn.classList.toggle('btn-primary', kind === 'primary');
  btn.classList.toggle('btn-secondary', kind === 'secondary');
  btn.querySelector('.spin').hidden = !busy;
  const ic = btn.querySelector('.ico-idle');
  if (busy) ic.setAttribute('hidden', ''); else ic.removeAttribute('hidden');
  if (ico) ic.querySelector('use').setAttribute('href', '#i-' + ico);
  if (label && btn.querySelector('.lbl').textContent !== label) btn.querySelector('.lbl').textContent = label;
}
function revealOnPhone(el) {
  if (!isPhone() || el.hidden || el.closest('[hidden]')) return;
  const r = el.getBoundingClientRect();
  if (r.top < 0 || r.top > innerHeight * .6) el.scrollIntoView({behavior: 'smooth', block: 'start'});
}

/* ================= build every option list from BOOT ================= */
const SRC_DECOR = {'Flatbed': {icon: 'flatbed'}, 'ADF': {icon: 'feeder', sub: 'Front side only'}, 'ADF Duplex': {icon: 'feeder2', sub: 'Both sides'}};
const SIDE_DECOR = {'None': {icon: 'simplex', sub: 'Front side only'}, 'DuplexNoTumble': {icon: 'book', sub: 'Like a book'}, 'DuplexTumble': {icon: 'pad', sub: 'Like a calendar'}};
function tileHTML(name, value, label, decor, checked, srParen) {
  const [b, paren] = splitLabel(label);
  let small = '';
  if (paren && srParen) small = `<span class="sr">（</span>${esc(paren)}<span class="sr">）</span>`;
  else if (paren) small = `(${esc(paren)})${decor.sub ? ' ' + esc(decor.sub) : ''}`;
  else if (decor.sub) small = esc(decor.sub);
  return `<label class="tile"><input type="radio" name="${name}" value="${esc(value)}"${checked ? ' checked' : ''}>
    <span class="t"><span class="ck"><svg class="i"><use href="#i-check"/></svg></span>
      ${icon(decor.icon || 'file')}<b>${esc(b)}</b>${small ? `<small>${small}</small>` : ''}</span></label>`;
}
const optionHTML = (list, sel) => list.map(([v, l]) => `<option value="${esc(v)}"${v === sel ? ' selected' : ''}>${esc(l)}</option>`).join('');
const pick = (list, want) => (list.some(o => o[0] === want) ? want : (list[0] || [''])[0]);
const DEF = {
  source: pick(opts('sources'), 'Flatbed'),
  size: pick(opts('sizes'), 'full'),
  res: pick(opts('res'), String(BOOT.res_default || '')),
  mode: pick(opts('modes'), 'Color'),
  paper: pick(opts('papers'), 'A4'),
  sides: pick(opts('sides'), 'None'),
};
$('#source-tiles').innerHTML = opts('sources').map(([v, l]) => tileHTML('source', v, l, SRC_DECOR[v] || {}, v === DEF.source, true)).join('');
$('#scan-size').innerHTML = optionHTML(opts('sizes'), DEF.size);
$('#mode-seg').innerHTML = opts('modes').map(([v, l]) => `<label><input type="radio" name="mode" value="${esc(v)}"${v === DEF.mode ? ' checked' : ''}><span>${esc(l)}</span></label>`).join('');
$('#res-seg').innerHTML = opts('res').map(([v]) => `<label><input type="radio" name="res" value="${esc(v)}"${v === DEF.res ? ' checked' : ''}><span>${esc(v)}${v === DEF.res ? '<em class="rec">Recommended</em>' : ''}</span></label>`).join('');
$('#paper').innerHTML = optionHTML(opts('papers'), DEF.paper);
$('#scale-seg').innerHTML = opts('scales').map(([v, l]) => { const [b, sub] = splitLabel(l);
  return `<label><input type="radio" name="scale" value="${esc(v)}"><span>${esc(b)}${sub ? `<em class="sub">${esc(sub)}</em>` : ''}</span></label>`; }).join('');
$('#sides-tiles').innerHTML = opts('sides').map(([v, l]) => tileHTML('sides', v, l, SIDE_DECOR[v] || {}, v === DEF.sides, false)).join('');
const RETAIN = Number(BOOT.retain_min) || 15;
$$('.retain-min').forEach(e => { e.textContent = RETAIN; });
const MAX_MB = Number(BOOT.max_upload_mb) || 120;
$('#max-mb').textContent = MAX_MB;
const ADF_MAX_W = Number(BOOT.adf_max_w) || 297;
const SIZE_CONFLICT_MSG = `The feeder accepts paper up to ${ADF_MAX_W} mm wide. Use the flatbed for larger sheets.`;
$('#size-warn-t').textContent = SIZE_CONFLICT_MSG;

/* ================= SCAN ================= */
const scanForm = $('#scan-form');
const RES_HINT = {
  100: '100 dpi: smallest file; overview quality',
  150: '150 dpi: small file; suitable for sharing',
  200: '200 dpi: suitable for everyday documents',
  300: '300 dpi: clear detail and moderate file size (recommended)',
};
/* the size hint must be true for the chosen source: the feeder never scans "the whole glass" */
/* What each size choice does. Scans are the scanner's own output: nothing is cropped, rotated or darkened.
   Feeder facts (measured): the scanner never sees ~2 mm at both window sides and at the leading end; a size with
   room beside the paper (BOOT.adf_side_room) keeps both side edges whole, a sheet as wide as the feed path cannot. */
const ADF_ROOM = BOOT.adf_side_room || {};
function sizeHint(src, size) {
  const adf = src !== 'Flatbed', name = labelOf('sizes', size).split(' / ')[0];
  if (size === 'full') return adf ? 'Scan the full 297 x 432 mm feeder area. The PDF can be much larger than the sheet. A3 or sideways A4 fills the feeder width, leaving unseen strips along both long edges. Select the actual paper size when known.'
                                  : 'Scan the entire glass without cropping. Use this default if unsure.';
  if (adf && (ADF_ROOM[size] == null || ADF_ROOM[size] >= 4)) return `Scan ${name} with about 5 mm extra on each side, including edge seals. The PDF is slightly larger than ${name}; about 2 mm at the leading edge remains unseen.`;
  if (adf) return `Scan ${name}. This size fills the feeder width, leaving a few millimeters unseen on each long edge. Inspect edge seals after scanning.`;
  return `Scan a ${name} area at the top-left of the glass. Align the sheet with that corner.`;
}
/* S.view: idle | busy | done | review | partial | error | restarted.  S.others: someone else holds the scanner (from /status). */
const S = {job: null, view: 'idle', data: {}, others: null, starting: false, legacy: false};
let netDown = false;

function scanSummary() {
  const f = scanForm.elements;
  const sizeV = f.size.value;
  const adf = f.source.value !== 'Flatbed';
  const sizeShort = sizeV === 'full' ? (adf ? 'Full area' : 'Entire glass') : labelOf('sizes', sizeV).split(' / ')[0];
  return {
    main: `${splitLabel(labelOf('sources', f.source.value))[0]} · ${sizeShort} · ${f.res.value} dpi · ${labelOf('modes', f.mode.value)}`,
    sub: f.paper_white && f.paper_white.checked ? 'Paper whitening / original saved separately' : 'Original scanner output',
  };
}
/* only if the feeder were narrower than A3 (it is not: 297 mm since 2026-10-19) */
const scanConflict = () => scanForm.elements.source.value !== 'Flatbed' && scanForm.elements.size.value === 'A3' && ADF_MAX_W < 297;
const pagesFromName = name => { const m = /-(\d+)p\.pdf$/.exec(name || ''); return m ? +m[1] : 0; };
/* server text, escaped; the file name inside it gets the non-breaking-inside treatment */
function msgHTML(msg, file) {
  const e = esc(msg);
  if (!file) return e;
  const ef = esc(file);
  return e.includes(ef) ? e.split(ef).join(`<span class="fname">${fileHTML(file)}</span>`) : e;
}
function pagesStrip(n, next) {
  const shown = Math.min(n, 10);
  if (!shown && !next) return '';
  return `<div class="pstrip" aria-hidden="true">${'<i class="pg"></i>'.repeat(shown)}${next ? '<i class="pg next"></i>' : ''}</div>`;
}
function splitSentence(msg) {                    // "原因。怎么办" → [原因。, 怎么办]
  const match = /[。.]\s*/.exec(msg);
  const cut = match ? match.index + match[0].length : 0;
  return cut > 0 && cut < msg.length ? [msg.slice(0, cut), msg.slice(cut)] : [msg, ''];
}
const SCAN_STATES = {
  idle: () => ({tone: 'idle', icon: 'scan', title: 'Scanner ready',
    detail: 'Place the paper, choose the source, then select Start scan.'}),
  busy: ({msg, pages = 0, phase} = {}) => ({tone: 'busy', spinner: true,
    title: `Your scan: ${esc(msg || 'Checking progress...')}`,
    detail: netDown ? esc(NET_ERR) : 'A PDF will be created after scanning. Keep this page open.',
    extra: pagesStrip(pages, phase !== 'processing'), progress: true}),
  occupied: ({msg, title} = {}) => ({tone: 'wait', icon: 'hourglass', title: esc(title || 'Scanner in use by another user'),
    detail: `${msg ? esc(msg.replace(/[。.]$/, '')) + '。' : ''}Start scan will become available when the other job finishes. No refresh is needed.`}),
  done: ({file, msg}) => ({tone: 'ok', icon: 'check',
    title: msg ? msgHTML(msg, file) : `Complete: <span class="fname">${fileHTML(file)}</span>`,
    actions: file ? [{label: 'Download PDF', icon: 'download', kind: 'primary', href: fileURL(file), download: file},
                     {label: 'Print this file', icon: 'printer', act: 'print-file', file}] : []}),
  review: ({file, msg} = {}) => ({tone: 'warn', icon: 'alert',
    title: 'Review required: the scan may be incomplete',
    detail: msgHTML(msg || 'Check the content, orientation and count of every page. Adjust paper orientation or size and rescan if needed.', file),
    actions: [...(file ? [{label: 'Download PDF for review', icon: 'download', kind: 'primary', href: fileURL(file), download: file},
                          {label: 'Review, then print', icon: 'printer', act: 'print-file', file}] : []),
              /* the sheet did not fit the chosen size: one tap rescans the whole feed path (the station waits for
                 the paper to be put back) */
              ...(/The sheet is wider than the selected size|The sheet is longer than the scan area/.test(msg || '')
                ? [{label: 'Rescan full area', icon: 'retry', act: 'scan-full', disabled: !!S.others}] : [])]}),
  partial: ({file, msg}) => ({tone: 'warn', icon: 'alert', title: msgHTML(msg || 'Only part of the scan completed', file),
    detail: 'Check the sheets remaining in the feeder and scan the missing pages.',
    actions: [...(file ? [{label: 'Download completed pages', icon: 'download', kind: 'primary', href: fileURL(file), download: file}] : []),
              {label: 'Scan remaining pages', icon: 'retry', act: 'scan', disabled: !!S.others}]}),
  error: ({msg} = {}) => {
    const [head, rest] = splitSentence(stripTrouble(msg) || 'Scanning did not finish');
    const feeder = /The feeder did not detect paper/.test(msg || '');
    return {tone: 'err', icon: 'alert-circle', title: `Error: ${esc(head)}`, detail: esc(rest), escalate: !feeder && !/administrator/.test(msg || ''),
      actions: [{label: 'Retry', icon: 'retry', kind: 'primary', act: 'scan', disabled: !!S.others},
                ...(feeder ? [{label: 'Use flatbed', icon: 'scan', act: 'scan-flatbed', disabled: !!S.others}] : [])]};
  },
  ended: ({msg} = {}) => ({tone: 'idle', icon: 'info', title: 'Scan ended',
    detail: `Completed PDFs appear in the refreshed Scanned PDFs list below. ${msg ? 'Station message: ' + esc(msg) : ''}`}),
  restarted: ({msg} = {}) => {
    const [head, rest] = splitSentence(msg);
    return {tone: 'warn', icon: 'alert', title: esc(head), detail: esc(rest),
      actions: [{label: 'Scan again', icon: 'retry', kind: 'primary', act: 'scan', disabled: !!S.others}]};
  },
};
const RESTART_SCAN_MSG = 'The station restarted before this scan finished. Recovered pages appear in Scanned PDFs. Review them before rescanning';
let lastScanBanner = '';
function scanBannerName() { return S.view === 'idle' && S.others ? 'occupied' : S.view; }
function renderScan() {
  const name = scanBannerName();
  renderBanner($('#scan-status'), SCAN_STATES[name](name === 'occupied' ? S.others : S.data));
  updateScan();
  if (name !== lastScanBanner) {
    if (name !== 'idle' && lastScanBanner) revealOnPhone($('#scan-status'));
    lastScanBanner = name;
  }
}
/** name: idle | busy | occupied | done | review | partial | error | restarted (UI hook; the poller drives it from /status) */
function setScanState(name, data = {}) {
  if (name === 'occupied') { S.others = data; if (S.view === 'busy') S.view = 'idle'; }
  else { S.view = name; S.data = data; }
  renderScan();
}
function updateScan() {
  const s = scanSummary(), f = scanForm.elements, conflict = scanConflict();
  const go = $('#scan-go'), dl = $('#scan-dl');
  $('#res-hint').textContent = RES_HINT[f.res.value] || `${f.res.value} dpi`;
  $('#size-hint').textContent = sizeHint(f.source.value, f.size.value);
  $('#size-warn').hidden = !conflict;
  const busy = S.view === 'busy';
  const locked = busy || S.starting;
  $('#scan-fields').disabled = locked;
  $('#scan-lock').hidden = !locked; $('#scan-meta').hidden = locked;
  const others = !busy && S.others;
  const block = conflict || !!others;
  let two = false;
  if (S.starting) {
    setButton(go, {disabled: true, busy: true, label: 'Starting...'});
    setBar('scan', {main: s.main, sub: 'Connecting to scanner...', tone: 'busy'});
  } else switch (S.view) {
    case 'busy': {
      const d = S.data, proc = d.phase === 'processing';
      setButton(go, {disabled: true, busy: true, label: proc ? 'Processing...' : 'Scanning...'});
      setBar('scan', netDown ? {main: proc ? `Processing ${d.pages || 0} pages` : `Scanning: ${d.pages || 0} pages scanned`, sub: NET_ERR, tone: 'err'}
        : {main: proc ? `Processing ${d.pages || 0} pages` : `Scanning: ${d.pages || 0} pages scanned`,
           sub: proc ? 'Creating the PDF. Keep this page open' : 'Do not move the paper while scanning', tone: 'busy'});
      break;
    }
    case 'done': {
      const file = S.data.file || '';
      two = !!file;
      if (file) { dl.href = fileURL(file); dl.setAttribute('download', file); }
      setButton(go, {disabled: block, label: 'Scan another', kind: file ? 'secondary' : 'primary', ico: 'scan'});
      const n = pagesFromName(file);
      setBar('scan', {main: n ? `Complete: ${n} pages` : 'Complete', sub: 'The file is in Scanned PDFs', tone: 'ok'});
      break;
    }
    case 'review': {
      const file = S.data.file || '';
      two = !!file;
      if (file) { dl.href = fileURL(file); dl.setAttribute('download', file); }
      setButton(go, {disabled: block, label: 'Rescan', kind: file ? 'secondary' : 'primary', ico: 'retry'});
      setBar('scan', {main: 'Review the scan', sub: 'Check contents, orientation and page count; rescan missing pages', tone: 'warn'});
      break;
    }
    case 'error':
      setButton(go, {disabled: block, label: 'Retry', ico: 'retry'});
      setBar('scan', {main: s.main, sub: 'See the red error message', tone: 'err'});
      break;
    case 'partial':
      setButton(go, {disabled: block, label: 'Scan remaining pages', ico: 'retry'});
      setBar('scan', {main: s.main, sub: 'Partial scan; see the amber notice', tone: 'warn'});
      break;
    case 'ended':
      setButton(go, {disabled: block, label: 'Scan another', ico: 'scan'});
      setBar('scan', {main: s.main, sub: 'Scan ended; see Scanned PDFs for the result'});
      break;
    case 'restarted':
      setButton(go, {disabled: block, label: 'Scan again', ico: 'retry'});
      setBar('scan', {main: s.main, sub: 'The station restarted; see the amber notice', tone: 'warn'});
      break;
    default:
      setButton(go, {disabled: block, label: 'Start scan', ico: 'scan'});
      setBar('scan', conflict ? {main: s.main, sub: 'Use the flatbed for paper that does not fit the feeder', tone: 'warn'} : {main: s.main, sub: s.sub});
  }
  if (!S.starting && !busy) {
    if (others && !conflict) setBar('scan', {sub: `${others.msg || 'Scanner in use by another user'}; available automatically when the other job finishes`, tone: 'warn'});
    else if (netDown) setBar('scan', {sub: NET_ERR, tone: 'err'});
  }
  dl.hidden = !two;
  $('#scan-bar').classList.toggle('two', two);
}
/* The default size follows the source until the user picks a size: feeder -> A4 (almost every feeder job is A4),
   glass -> the whole glass. */
let sizeTouched = false;
const sizeDefaultFor = src => (src === 'Flatbed' ? 'full' : 'A4');
$('#scan-size').addEventListener('change', () => { sizeTouched = true; });
scanForm.addEventListener('change', e => {
  if (e.target && e.target.name === 'source' && !sizeTouched) scanForm.elements.size.value = sizeDefaultFor(e.target.value);
});
/* The scan choices survive a reload of this tab (F5, a phone restoring the tab, an update reload): a restored
   result's Rescan must use the same source and size, not the glass default. */
function saveScanForm() {
  const f = scanForm.elements;
  store.set('scan-form', {source: f.source.value, size: f.size.value, res: f.res.value, mode: f.mode.value, touched: sizeTouched,
                         pw: !!(f.paper_white && f.paper_white.checked)});
}
function restoreScanForm(v) {
  if (!v || typeof v !== 'object') return;
  const has = (list, x) => opts(list).some(o => o[0] === x);
  if (has('sources', v.source)) setRadio(scanForm, 'source', v.source);
  if (has('sizes', v.size)) scanForm.elements.size.value = v.size;
  if (has('res', v.res)) setRadio(scanForm, 'res', v.res);
  if (has('modes', v.mode)) setRadio(scanForm, 'mode', v.mode);
  if (typeof v.pw === 'boolean' && scanForm.elements.paper_white) scanForm.elements.paper_white.checked = v.pw;
  sizeTouched = !!v.touched;
}
scanForm.addEventListener('change', saveScanForm);
scanForm.addEventListener('change', updateScan);
scanForm.addEventListener('submit', e => { e.preventDefault(); startScan(); });

async function startScan() {
  if (S.starting || S.view === 'busy' || S.others || scanConflict()) {
    if (scanConflict()) revealOnPhone($('#size-warn'));
    return;
  }
  const body = new FormData(scanForm);          // read the fields BEFORE the fieldset gets disabled
  body.append('build', BOOT.build || '');       // the station refuses a page from another build
  saveScanForm();
  S.starting = true;
  renderScan();
  const r = await api('/scan', {method: 'POST', body});
  S.starting = false;
  if (r.status === 400 && r.data && r.data.stale) {   // updated since this page loaded: reload, keep the choices
    if (!reloadForUpdate()) { S.view = 'error'; S.data = {msg: r.error}; renderScan(); }   // printing: say so
    return;
  }
  if (r.ok && r.data.job) {
    store.set('scan-review', null);
    S.job = {id: String(r.data.job), running: true};
    store.set('scan', S.job);
    S.view = 'busy'; S.data = {msg: '', pages: 0, phase: 'scanning'};
    renderScan();
    schedulePoll();
  } else if (r.status === 202) {                // older station software: "started", no job id → follow scanner.busy
    store.set('scan-review', null);
    S.legacy = true;
    S.view = 'busy'; S.data = {msg: 'Scan started', pages: 0, phase: 'scanning'};
    renderScan();
    schedulePoll();
  } else if (r.status === 409) {                // someone else got the scanner first
    S.others = {title: r.data && r.data.error ? r.error : '', msg: ''};
    renderScan();
    schedulePoll();
  } else {
    S.view = 'error'; S.data = {msg: r.error};
    renderScan();
  }
}

/* ---------- scanned files (GET /files → [{name, kb, left_min}]) ---------- */
let files = [], freshName = null, armed = null;   // armed = {name, t}: first tap of the two-tap delete
function setFiles(list, {fresh} = {}) {
  files = Array.isArray(list) ? list.filter(f => f && typeof f.name === 'string') : [];
  if (fresh !== undefined) freshName = fresh;
  renderFiles();
}
const fmtSize = kb => { kb = Number(kb) || 0; return kb >= 1024 ? `${(kb / 1024).toFixed(kb >= 10240 ? 0 : 1)} MB` : `${Math.max(1, kb)} KB`; };
function qualityMessage(quality) {
  if (!quality || !['review', 'partial'].includes(quality.state)) return '';
  const issues = Array.isArray(quality.issues) ? quality.issues.filter(s => typeof s === 'string' && s.trim()).join(quality.kind === 'office_conversion' ? ' ' : '；') : '';
  const title = quality.kind === 'office_conversion' ? 'Review required: conversion may change the layout'
    : quality.state === 'partial' ? 'Partial scan; review required' : 'Review required: the scan may be incomplete';
  return title + (issues ? '。' + issues : '. Check the content, orientation and count of every page.');
}
function renderFiles() {
  const ul = $('#files');
  $('#files-count').textContent = files.length;
  $('#files-empty').hidden = files.length > 0;
  ul.hidden = files.length === 0;
  ul.innerHTML = files.map(f => {
    const left = Math.max(0, Number(f.left_min) || 0);
    const isNew = f.name === freshName, soon = left <= 3;
    const isArmed = armed && armed.name === f.name;
    const quality = qualityMessage(f.quality);
    return `
    <li class="file${isNew ? ' is-new' : ''}${quality ? ' is-review' : ''}" data-name="${esc(f.name)}">
      <span class="ftype" aria-hidden="true">PDF</span>
      <div class="f-head"><a class="f-name" href="${esc(fileURL(f.name))}" target="_blank" rel="noopener" title="Open or download PDF"><span class="f-text">${fileHTML(f.name)}</span>${icon('download')}</a>${quality ? '<span class="badge-new">Review required</span>' : isNew ? '<span class="badge-new">New scan</span>' : ''}</div>
      <p class="f-meta"><span>${esc(fmtSize(f.kb))}</span><span class="ttl${soon ? ' soon' : ''}">${icon('clock')}${left < 1 ? 'Less than 1 minute remaining' : `${left} minutes remaining`}${soon ? ' / expiring soon' : ''}</span>${f.small ? `<a class="f-small" href="/small/${esc(encodeURIComponent(f.name))}" download>Smaller email copy</a>` : ''}${f.raw ? `<a class="f-small" href="/raw/${esc(encodeURIComponent(f.name))}" download>Original scan</a>` : ''}${quality ? `<span class="f-review">${esc(quality)}</span>` : ''}</p>
      <div class="f-acts">
        <button type="button" class="btn btn-secondary btn-sm" data-act="print-file" data-file="${esc(f.name)}">${icon('printer')}${quality ? 'Review, then print' : 'Print'}</button>
        <button type="button" class="btn btn-danger btn-sm${isArmed ? ' is-armed' : ''}" data-act="delete" data-file="${esc(f.name)}" aria-label="${isArmed ? 'Click again to delete ' : 'Delete '}${esc(f.name)}">${icon('trash')}<span>${isArmed ? 'Delete this file?' : 'Delete'}</span></button>
      </div>
    </li>`;
  }).join('');
}
let filesSeq = 0;
async function refreshFiles() {
  const my = ++filesSeq;
  const r = await api('/files');
  if (my !== filesSeq || !r.ok) return;
  setFiles(r.data.files);
}
async function deleteFile(btn) {
  const name = btn.dataset.file;
  // two-tap confirm (no modal): first tap arms, second tap within 4 s deletes.
  if (!(armed && armed.name === name)) {
    if (armed) clearTimeout(armed.t);
    armed = {name, t: setTimeout(() => { armed = null; renderFiles(); }, 4000)};
    renderFiles();
    return;
  }
  clearTimeout(armed.t); armed = null;
  btn.disabled = true;
  const li = btn.closest('.file');
  const r = await api('/delete', {method: 'POST', body: new URLSearchParams({name})});
  if (!r.ok) {
    renderFiles();
    const row = $$('#files .file').find(x => x.dataset.name === name);
    if (row) row.querySelector('.f-meta').insertAdjacentHTML('beforeend', `<span class="f-err">Deletion failed: ${esc(r.error)}</span>`);
    return;
  }
  if (name === freshName) freshName = null;
  li.classList.add('is-leaving');
  setTimeout(refreshFiles, 250);
}

/* ================= PRINT ================= */
const printForm = $('#print-form');
const pagesInput = $('#pages');
const copiesInput = $('#copies');
const thumbsEl = $('#thumbs');
const CAP_AT = 18;              // more pages than this → the grid becomes its own scroll box
const NONE_TEXT = 'No pages selected';   // shown in the page field when nothing is selected (empty would mean "all")
/* P.busy = POST /print in flight (never a second tap = duplicate paper) */
const P = {doc: null, sel: new Set(), rangeError: null, converting: false, busy: false, state: 'none', data: {}, retry: false, note: '', kind: 'upload', lastUse: 0};
const PJ = {job: null, show: false};   // my latest print job {id, running}; show = its state owns the print banner
/* P.lastUse: when the loaded document was last touched; the station deletes it after RETAIN idle minutes, and an
   update reload waits while it is in use */
['pointerdown', 'keydown', 'wheel'].forEach(t => document.addEventListener(t, e => {
  if (P.doc && e.target && e.target.closest && e.target.closest('#panel-print, #lightbox')) P.lastUse = Date.now();
}, {capture: true, passive: true}));
/* a newer local message (upload/convert error, new document, print request error) takes the banner away from an older job;
   only a NEW print job id can take it back */
function showLocal(name, data) { PJ.show = false; setPrintState(name, data); }
let docSeq = 0;                  // newest /upload or /copy wins; older responses are dropped

/* keep the result banner next to 打印: top of the panel on phones, inside the settings card on desktop */
function placePrintStatus() {
  const st = $('#print-status');
  if (deskMQ.matches) printForm.insertBefore(st, $('#print-bar'));
  else $('#panel-print').insertBefore(st, $('#print-grid'));
}
if (deskMQ.addEventListener) deskMQ.addEventListener('change', placePrintStatus); else if (deskMQ.addListener) deskMQ.addListener(placePrintStatus);   // Safari < 14
placePrintStatus();

/** Page-range parser — the same rules as the server's parse_page_ranges():
    full-width digits → ASCII; ，、；; → ,  －—–~～到至 → -  第/页 removed; whitespace/commas separate;
    a-b ranges (reversed allowed); 全部/all = all; empty = all.  Returns {set} or {error} (server wording). */
const WS = '[\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]';   // Python str.isspace()
const RE_WS_EDGE = new RegExp(`^${WS}+|${WS}+$`, 'g');
const RE_DASH = new RegExp(`${WS}*-${WS}*`, 'g');
const RE_SEP = new RegExp(`(?:${WS}|,)+`, 'g');
/* Unicode digit classes (\p{Nd}) need ES2018 regexps: build them at run time so an older browser falls back to 0-9 instead of failing to load */
let RE_PART = /^([0-9]{1,5})(?:-([0-9]{1,5}))?$/, RE_ND = /[0-9]/;
try { RE_PART = new RegExp('^(\\p{Nd}{1,5})(?:-(\\p{Nd}{1,5}))?$', 'u'); RE_ND = new RegExp('\\p{Nd}', 'u'); } catch (e) { /* ASCII digits only */ }
const REPL = [['，', ','], ['、', ','], ['；', ','], [';', ','], ['－', '-'], ['—', '-'], ['–', '-'], ['~', '-'], ['～', '-'],
              ['到', '-'], ['至', '-'], ['第', ''], ['页', '']];
/* int() of any Unicode decimal digits, like Python (Nd digits come in contiguous 0–9 runs) */
function ndInt(str) {
  let v = 0;
  for (const ch of str) {
    let cp = ch.codePointAt(0), z = cp;
    while (z > 0 && RE_ND.test(String.fromCodePoint(z - 1))) z--;
    v = v * 10 + ((cp - z) % 10);
  }
  return v;
}
function parseRange(spec, total) {
  let s = String(spec == null ? '' : spec).replace(RE_WS_EDGE, '');
  if ([...s].length > 4000) return {error: 'Page selection is too long. Use a compact range such as 1-3,5,8-10'};
  s = s.replace(/[０-９]/g, c => String.fromCharCode(c.charCodeAt(0) - 0xFEE0));
  for (const [a, b] of REPL) s = s.split(a).join(b);
  s = s.replace(RE_DASH, '-').replace(RE_SEP, ',').replace(/^,+|,+$/g, '');
  const all = () => ({set: new Set(Array.from({length: total}, (_, i) => i + 1))});
  if (['', '全部', 'all'].includes(s.toLowerCase())) return all();
  const set = new Set();
  for (const part of s.split(',')) {
    const m = RE_PART.exec(part);
    if (!m) return {error: `Invalid page selection ${[...part].slice(0, 20).join('')}. Use a range such as 1-3,5,8-10`};
    let a = ndInt(m[1]), b = m[2] ? ndInt(m[2]) : a;
    if (a > b) [a, b] = [b, a];
    if (a < 1 || b > total) return {error: `This file has ${total} pages; selection ${part} is out of range`};
    for (let i = a; i <= b; i++) set.add(i);
  }
  return {set};
}
function formatRange(set) {
  const a = [...set].sort((x, y) => x - y), out = [];
  for (let i = 0; i < a.length;) {
    let j = i;
    while (j + 1 < a.length && a[j + 1] === a[j] + 1) j++;
    out.push(i === j ? `${a[i]}` : `${a[i]}-${a[j]}`);
    i = j + 1;
  }
  return out.join(',');
}
const copies = () => Math.min(200, Math.max(1, parseInt(copiesInput.value, 10) || 1));
const sides = () => printForm.elements.sides.value;
const scaleV = () => printForm.elements.scale.value;
const sheetsPerCopy = n => sides() !== 'None' ? Math.ceil(n / 2) : n;
const sideShort = v => splitLabel(labelOf('sides', v))[0];

function printBarForState(main) {
  switch (P.state) {
    case 'sent': return {main: 'Sent to printer', sub: netDown ? NET_ERR : 'Do not resubmit while the printer is receiving the job', tone: netDown ? 'err' : 'busy'};
    case 'blocked': return {main: main || 'Printing paused', sub: 'Printer issue; see the amber notice', tone: 'warn'};
    case 'warn': return {main: main || 'Print result unknown', sub: 'See the amber notice', tone: 'warn'};
    case 'restarted': return {main: main || 'Station restarted', sub: 'See the amber notice', tone: 'warn'};
    case 'received': return {main: main || 'Printer received the job', sub: 'The printer received the job; check and collect the pages', tone: 'ok'};
    case 'cancelled': return {main: main || 'Cancelled', sub: 'Cancelled; see the message above'};
    case 'error': return {main: main || 'No file selected', sub: 'See the red error message', tone: 'err'};
    default: return null;
  }
}
function updatePrint() {
  const box = $('#pages-sum'), btn = $('#print-go');
  const setBox = (cls, ic, html) => { const c = 'sumbox ' + cls, h = icon(ic) + `<span>${html}</span>`;
    if (box.className !== c) box.className = c; if (box._h !== h) { box._h = h; box.innerHTML = h; } };
  const d = P.doc;
  $('#sel-all').disabled = $('#sel-none').disabled = !d || P.converting || P.busy;
  pagesInput.disabled = !d || P.converting || P.busy;
  thumbsEl.classList.toggle('is-stale', !!(d && P.rangeError));
  thumbsEl.classList.toggle('is-locked', P.busy);
  const retry = P.state === 'error' && P.retry;
  if (!d || P.converting) {
    setBox('is-muted', 'info', 'After preview generation, enter page numbers here or use the thumbnail checkmarks.');
    setButton(btn, {disabled: true, label: 'Print', ico: 'printer'});
    if (P.converting) setBar('print', {main: 'Creating preview...', sub: 'Please wait while the Office file is converted'});
    else setBar('print', printBarForState('') && P.state !== 'error' ? printBarForState('')
      : P.state === 'error' ? {main: 'No file selected', sub: 'See the red error message', tone: 'err'} : {main: 'No file selected', sub: 'Choose a file to print first'});
    return;
  }
  if (P.rangeError) {
    pagesInput.setAttribute('aria-invalid', 'true');
    setBox('is-err', 'alert-circle', `${esc(P.rangeError)}<small>The selection updates after the page range is corrected</small>`);
    setButton(btn, {disabled: true, label: 'Print', ico: 'printer'});
    setBar('print', {main: 'Invalid page selection', sub: 'Correct Pages to print before printing', tone: 'err'});
    return;
  }
  pagesInput.removeAttribute('aria-invalid');
  const n = P.sel.size, per = sheetsPerCopy(n);
  const which = n === d.pages ? 'All' : `Pages ${formatRange(P.sel)}`;
  if (n === 0) {
    setBox('is-warn', 'alert', 'No pages selected. Use the thumbnail checkmarks or Select all');
    setButton(btn, {disabled: true, label: 'Print', ico: 'printer'});
    setBar('print', {main: 'No pages selected', sub: 'Select at least one page to print', tone: 'warn'});
    return;
  }
  setBox('', 'pages', `Selected <b>${n}</b> of ${d.pages} pages (${which}) / about <b>${per}</b> sheets per copy`);
  setButton(btn, {disabled: P.busy, busy: P.busy, label: P.busy ? 'Sending...' : retry ? 'Retry' : 'Print', ico: retry ? 'retry' : 'printer'});
  const main = `${n} pages x ${copies()} copies / about ${per * copies()} sheets`;
  const sub = `${printForm.elements.paper.value} · ${sideShort(sides())} / Monochrome`;
  setBar('print', printBarForState(main) || {main, sub});
  if (lbIsOpen()) renderLightbox();
}

/* thumbnails: one card per page, real previews lazy-loaded from GET /preview/<token>?p=<0-based> */
function renderThumbs(count, {skeleton = false, token = ''} = {}) {
  const capped = count > CAP_AT;
  thumbsEl.classList.toggle('capped', capped);
  thumbsEl.scrollTop = 0;
  const t = encodeURIComponent(token);
  let html = '';
  for (let p = 1; p <= count; p++) {
    html += skeleton
      ? `<div class="thumb is-skel" data-page="${p}" aria-hidden="true"><span class="sheet"><span class="paper is-loading"></span></span><span class="cap">Page ${p}</span></div>`
      : `<div class="thumb" data-page="${p}" data-on="true">
      <span class="sheet">
        <button type="button" class="t-open" aria-label="Enlarge page ${p}"><span class="paper is-loading"><img loading="lazy" decoding="async" alt="" ${NATIVE_LAZY || !lazyIO ? 'src' : 'data-src'}="/preview/${t}?p=${p - 1}"></span><span class="stamp" aria-hidden="true">Excluded</span><span class="zoom" aria-hidden="true">${icon('zoom')}Enlarge</span></button>
        <button type="button" class="tick" aria-pressed="true" aria-label="Print page ${p}"><span class="dot">${icon('check')}</span></button>
      </span>
      <span class="cap" aria-hidden="true">Page ${p}</span></div>`;
  }
  thumbsEl.innerHTML = html;
  if (!skeleton && !NATIVE_LAZY && lazyIO) {
    lazyIO.disconnect();
    $$('img[data-src]', thumbsEl).forEach(i => lazyIO.observe(i));
  }
  requestAnimationFrame(updateMore);
}
/* fallback for browsers without loading=lazy (e.g. Safari < 15.4): set src when the card comes near the viewport */
const lazyIO = !NATIVE_LAZY && 'IntersectionObserver' in window ? new IntersectionObserver(entries => {
  entries.forEach(en => {
    if (!en.isIntersecting) return;
    const img = en.target;
    img.src = img.getAttribute('data-src'); img.removeAttribute('data-src');
    lazyIO.unobserve(img);
  });
}, {rootMargin: '600px 0px'}) : null;
/* image load / error don't bubble — listen in the capture phase */
thumbsEl.addEventListener('load', e => {
  if (e.target.tagName === 'IMG') e.target.parentElement.classList.remove('is-loading');
}, true);
thumbsEl.addEventListener('error', e => {
  const img = e.target;
  if (img.tagName !== 'IMG') return;
  const paper = img.parentElement;
  paper.classList.remove('is-loading');
  img.remove();
  paper.insertAdjacentHTML('beforeend', `<span class="pv-fail" title="Preview unavailable. This page can still be selected and printed">${icon('alert-circle')}Preview failed</span>`);
}, true);
/* long documents: fade the bottom edge and say "框内可以上下滚动" while more pages sit below */
function updateMore() {
  const capped = thumbsEl.classList.contains('capped');
  const more = capped && thumbsEl.scrollTop + thumbsEl.clientHeight < thumbsEl.scrollHeight - 4;
  thumbsEl.classList.toggle('more', more);
  if (P.doc) $('#doc-hint').textContent = capped ? `${P.doc.pages} pages; scroll inside this panel` : '';
}
thumbsEl.addEventListener('scroll', updateMore, {passive: true});
window.addEventListener('resize', () => thumbsEl.hidden || updateMore());
function syncThumbs() {
  $$('.thumb:not(.is-skel)', thumbsEl).forEach(t => {
    const p = t.dataset.page, on = String(P.sel.has(+p));
    if (t.dataset.on === on) return;
    t.dataset.on = on;
    t.querySelector('.tick').setAttribute('aria-pressed', on);
    t.querySelector('.t-open').setAttribute('aria-label', `Enlarge page ${p}${on === 'true' ? '' : ' (excluded)'}`);
  });
}
function syncInputFromSel() {
  pagesInput.value = P.sel.size === P.doc.pages ? '' : P.sel.size === 0 ? NONE_TEXT : formatRange(P.sel);
  P.rangeError = null;
}
function togglePage(p) {
  if (!P.doc || P.busy || P.rangeError || p < 1 || p > P.doc.pages) return;
  if (P.sel.has(p)) P.sel.delete(p); else P.sel.add(p);
  syncThumbs(); syncInputFromSel(); updatePrint();
}
/* corner badge = select / 不打印; the page itself = open the lightbox (looking never changes the selection) */
thumbsEl.addEventListener('click', e => {
  const tick = e.target.closest('.tick'), open = e.target.closest('.t-open');
  const t = e.target.closest('.thumb');
  if (!t || t.classList.contains('is-skel') || !P.doc || P.rangeError) return;
  if (tick) { e.stopPropagation(); togglePage(+t.dataset.page); }
  else if (open) openLightbox(+t.dataset.page);
});
/* ---------- page lightbox ---------- */
const lb = $('#lightbox');
const LB = {page: 0, seq: 0, from: null};
const pvURL = (p, hi) => `/preview/${encodeURIComponent(P.doc.token)}?p=${p - 1}${hi ? '&hi=1' : ''}`;
function loadInto(img, url, done) {
  img.onload = () => done(true);
  img.onerror = () => done(false);
  if (img.getAttribute('src') !== url) img.src = url;
  else if (img.complete) done(img.naturalWidth > 0);
}
function renderLightbox() {
  if (!P.doc || !LB.page) return;
  const p = LB.page, n = P.doc.pages, on = P.sel.has(p);
  $('#lb-title').innerHTML = `Page ${p}<span>/ ${n} pages</span>`;
  const st = $('#lb-state');
  st.className = 'lb-state ' + (on ? 'is-on' : 'is-off');
  st.innerHTML = on ? `${icon('check')}Included` : `${icon('x')}Excluded`;
  $('#lb-prev').disabled = p <= 1;
  $('#lb-next').disabled = p >= n;
  const tg = $('#lb-toggle');
  tg.disabled = P.busy;
  tg.className = 'btn lb-toggle ' + (on ? 'btn-secondary' : 'btn-primary');
  const html = on ? `${icon('x')}<span>Exclude this page</span>` : `${icon('check')}<span>Include this page</span>`;
  if (tg._h !== html) { tg._h = html; tg.innerHTML = html; }
  $('#lb-page').classList.toggle('is-off', !on);
  $('#lb-hi').alt = `Preview of page ${p}${on ? '' : ' (excluded from printing)'}`;
}
function showLightboxPage(p) {
  if (!P.doc) return;
  p = Math.min(Math.max(1, p), P.doc.pages);
  LB.page = p;
  const my = ++LB.seq, box = $('#lb-page'), st = {lo: 0, hi: 0};
  const upd = () => {
    if (my !== LB.seq) return;
    box.classList.toggle('hi-ready', st.hi === 1);
    box.classList.toggle('lo-ready', st.lo === 1 && st.hi !== 1);
    box.classList.toggle('lo-only', st.lo === 1 && st.hi === -1);
    box.classList.toggle('failed', st.lo === -1 && st.hi === -1);
    box.classList.toggle('is-loading', st.hi === 0 && st.lo !== 1);
  };
  box.classList.remove('hi-ready', 'lo-ready', 'lo-only', 'failed');
  box.classList.add('is-loading');
  loadInto($('#lb-lo'), pvURL(p, false), okk => { st.lo = okk ? 1 : -1; upd(); });
  loadInto($('#lb-hi'), pvURL(p, true), okk => {
    st.hi = okk ? 1 : -1; upd();
    if (okk && my === LB.seq && p < P.doc.pages) { const pre = new Image(); pre.src = pvURL(p + 1, true); }   // next page ready when Next page is tapped
  });
  renderLightbox();
}
const HAS_DIALOG = typeof lb.showModal === 'function';
const lbIsOpen = () => (HAS_DIALOG ? lb.open : lb.hasAttribute('open'));
function openLightbox(p) {
  if (!P.doc) return;
  LB.from = document.activeElement;
  showLightboxPage(p);
  if (lbIsOpen()) return;
  document.documentElement.classList.add('lb-open');
  if (HAS_DIALOG) lb.showModal();
  else {                                         // old browsers: same look, fixed layer + our own backdrop
    lb.setAttribute('open', ''); lb.classList.add('lb-fallback'); $('#lb-backdrop').hidden = false;
    $('#lb-close').focus();
  }
}
function closeLightbox() {
  if (!lbIsOpen()) return;
  if (HAS_DIALOG) { lb.close(); return; }
  lb.removeAttribute('open'); lb.classList.remove('lb-fallback'); $('#lb-backdrop').hidden = true;
  onLightboxClosed();
}
function lbGo(d) {
  if (!P.doc || !LB.page) return;
  const p = LB.page + d;
  if (p < 1 || p > P.doc.pages) return;
  showLightboxPage(p);
  const a = document.activeElement;
  if (a && a.disabled) (d > 0 ? $('#lb-prev') : $('#lb-next')).focus();
}
function onLightboxClosed() {
  document.documentElement.classList.remove('lb-open');
  const p = LB.page;
  LB.page = 0; LB.seq++;
  const t = p && thumbsEl.querySelector(`.thumb[data-page="${p}"] .t-open`);
  if (t) { t.focus({preventScroll: true}); t.scrollIntoView({block: 'nearest'}); }
}
lb.addEventListener('close', onLightboxClosed);
lb.addEventListener('click', e => { if (e.target === lb) closeLightbox(); });      // tap on the dimmed backdrop
$('#lb-backdrop').addEventListener('click', closeLightbox);
lb.addEventListener('keydown', e => {
  if (e.key === 'ArrowLeft') { e.preventDefault(); lbGo(-1); }
  else if (e.key === 'ArrowRight') { e.preventDefault(); lbGo(1); }
  else if (e.key === 'Escape' && !HAS_DIALOG) { e.preventDefault(); closeLightbox(); }
});
$('#lb-close').addEventListener('click', closeLightbox);
$('#lb-prev').addEventListener('click', () => lbGo(-1));
$('#lb-next').addEventListener('click', () => lbGo(1));
$('#lb-toggle').addEventListener('click', () => { togglePage(LB.page); renderLightbox(); });
/* swipe left / right on phones to turn pages */
(() => {
  let x0 = null, y0 = 0;
  const body = $('#lb-body');
  body.addEventListener('touchstart', e => { if (e.touches.length === 1) { x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; } else x0 = null; }, {passive: true});
  body.addEventListener('touchend', e => {
    if (x0 == null) return;
    const dx = e.changedTouches[0].clientX - x0, dy = e.changedTouches[0].clientY - y0;
    x0 = null;
    if (Math.abs(dx) > 50 && Math.abs(dx) > 1.5 * Math.abs(dy)) lbGo(dx < 0 ? 1 : -1);
  }, {passive: true});
})();

function onPagesInput() {
  if (!P.doc) return;
  if (pagesInput.value === NONE_TEXT) { P.rangeError = null; P.sel = new Set(); syncThumbs(); updatePrint(); return; }
  const r = parseRange(pagesInput.value, P.doc.pages);
  P.rangeError = r.error || null;
  if (r.set) { P.sel = r.set; syncThumbs(); }
  updatePrint();
}
pagesInput.addEventListener('input', onPagesInput);
/* tidy the typed text once the user leaves the box (２－４，７ → 2-4,7) */
pagesInput.addEventListener('change', () => { if (P.doc && !P.rangeError && pagesInput.value.trim()) syncInputFromSel(); });
/* "No pages selected" is a state, not text to edit: typing replaces it */
pagesInput.addEventListener('focus', () => { if (pagesInput.value === NONE_TEXT) pagesInput.select(); });
/* Enter / Go on a phone keyboard only applies the value — it must never print */
[pagesInput, copiesInput].forEach(el => el.addEventListener('keydown', e => {
  if (e.key === 'Enter' || e.keyCode === 13) { e.preventDefault(); el.blur(); }
}));
$('#sel-all').addEventListener('click', () => { if (!P.doc) return; P.sel = new Set(Array.from({length: P.doc.pages}, (_, i) => i + 1)); syncThumbs(); syncInputFromSel(); updatePrint(); });
$('#sel-none').addEventListener('click', () => { if (!P.doc) return; P.sel = new Set(); syncThumbs(); syncInputFromSel(); updatePrint(); });
$('#copies-minus').addEventListener('click', () => { copiesInput.value = Math.max(1, copies() - 1); onCopies(); });
$('#copies-plus').addEventListener('click', () => { copiesInput.value = Math.min(200, copies() + 1); onCopies(); });
copiesInput.addEventListener('change', () => { copiesInput.value = copies(); onCopies(); });
copiesInput.addEventListener('input', onCopies);
function onCopies() {
  $('#copies-minus').disabled = copies() <= 1;
  $('#copies-plus').disabled = copies() >= 200;
  updatePrint();
}
printForm.addEventListener('change', e => {
  if (e.target.name === 'paper') setPaperHint(null);
  if (e.target.name === 'scale') { updateScaleHint(); refreshSizeHint(); }
  if (e.target.name !== 'pages') updatePrint();
});
/* never a form post: only an explicit click/tap on 打印 prints */
printForm.addEventListener('submit', e => e.preventDefault());

const SCALE_HINT = {
  auto: 'Print at actual size; shrink proportionally only when a page is larger than the paper.',
  fit: 'Shrink the entire page slightly (about 4%) to include content near the edges.',
};
function updateScaleHint() { const v = scaleV(); $('#scale-hint').textContent = SCALE_HINT[v] || labelOf('scales', v); }
function setRadio(form, name, value) {
  const r = $$(`input[name="${name}"]`, form).find(x => x.value === value) || $(`input[name="${name}"]`, form);
  if (r) r.checked = true;
}

function typeOf(name) {
  const ext = (name.split('.').pop() || '').toLowerCase();
  if (ext === 'pdf') return 'PDF';
  if (/^(doc|docx|wps|odt|rtf|txt)$/.test(ext)) return 'DOC';
  if (/^(xls|xlsx|et|ods|csv)$/.test(ext)) return 'XLS';
  if (/^(ppt|pptx|dps|odp)$/.test(ext)) return 'PPT';
  return 'IMG';
}
function showDocbar(name, metaText) {
  $('#drop').hidden = true;
  $('#docbar').hidden = false;
  const t = typeOf(name);
  $('#doc-type').textContent = t;
  $('#doc-type').className = 'ftype' + (t === 'PDF' ? '' : ' doc');
  $('#doc-name').textContent = name;
  $('#doc-meta').textContent = metaText;
}
/** "uploading / converting" state (POST /upload or /copy in flight): no token, 打印 disabled. */
function setConverting(name, pagesGuess = 8) {
  closeLightbox();
  P.doc = null; P.converting = true; P.sel = new Set(); P.rangeError = null;
  $('#print-token').value = '';
  pagesInput.value = '';
  $('#doc-title').textContent = 'Preparing preview';
  $('#doc-hint').textContent = '';
  showDocbar(name, '');
  $('#doc-progress').hidden = false;
  $('#doc-sub').hidden = false;
  $('#doc-sub').textContent = 'Creating preview... Office files must be converted first and may take several seconds';
  $('#pagesel').hidden = false;
  $('#thumbs-head').hidden = true;
  thumbsEl.hidden = false;
  setPaperHint(null); setPaperNote(null);
  setQualityNote(null);
  renderThumbs(Math.max(1, Math.min(pagesGuess, CAP_AT)), {skeleton: true});
  updatePrint();
}
/* paper sizes in mm (portrait) for the informational hints */
const SIZE_MM = {A4: [210, 297], A3: [297, 420], A5: [148, 210], B5: [176, 250], Letter: [216, 279], Legal: [216, 356]};
function docMM(size) {
  if (SIZE_MM[size]) return SIZE_MM[size];
  const m = /^(\d+(?:\.\d+)?)\s*[×x*]\s*(\d+(?:\.\d+)?)\s*mm$/i.exec(size || '');
  return m ? [+m[1], +m[2]].sort((a, b) => a - b) : null;
}
let sizeHintLive = false;          // the doc-size hint is still showing (user hasn't touched 纸张)
function sizeHintText(size) {
  if (!size || size === 'A4' || printForm.elements.paper.value !== 'A4') return null;
  const mm = docMM(size);
  if (mm && mm[0] <= 212 && mm[1] <= 299) {      // smaller than A4: nothing gets shrunk
    return scaleV() === 'fit' ? `The document is ${size}; it will be enlarged proportionally to fit A4` : `The document is ${size}; it will print at actual size on A4`;
  }
  return `The document is ${size}; it will be reduced proportionally to fit A4`;
}
function refreshSizeHint() {
  if (!sizeHintLive || !P.doc) return;
  const t = sizeHintText(P.doc.size);
  if (t) setPaperHint(t, {info: true}); else setPaperHint(null);
}
/** Load a document {name, pages, size, token} (response of POST /upload or /copy), or null for the empty state.
    opts.kind: 'upload' (scale auto) | 'copy' (scale fit — a scan is full-bleed, the printer can't print the outer 4.2 mm) */
function loadDocument(d, {kind = 'upload'} = {}) {
  closeLightbox();
  P.converting = false; P.rangeError = null;
  $('#doc-progress').hidden = true;
  $('#doc-sub').hidden = true;
  setPaperHint(null); setPaperNote(null);
  setQualityNote(null);
  if (!d) {
    P.doc = null; P.sel = new Set(); pagesInput.value = ''; $('#print-token').value = '';
    $('#drop').hidden = false; $('#docbar').hidden = true; $('#pagesel').hidden = true;
    $('#thumbs-head').hidden = true; thumbsEl.hidden = true; thumbsEl.innerHTML = '';
    thumbsEl.classList.remove('capped', 'more');
    $('#doc-title').textContent = 'Choose a file to print';
    $('#doc-hint').textContent = '';
    updatePrint();
    return;
  }
  const pages = Math.max(1, parseInt(d.pages, 10) || 1);
  d = {name: String(d.name || ''), pages, size: String(d.size || ''), token: String(d.token || ''), quality: d.quality};
  setQualityNote(d.quality);
  P.doc = d; P.kind = kind; P.lastUse = Date.now();
  $('#print-token').value = d.token;
  P.sel = new Set(Array.from({length: pages}, (_, i) => i + 1));
  pagesInput.value = '';
  // every new document starts from the defaults: 1 copy, first side option (单面), scale per source, A4
  copiesInput.value = 1; onCopies();
  setRadio(printForm, 'sides', DEF.sides);
  setRadio(printForm, 'scale', kind === 'copy' ? 'fit' : 'auto');
  updateScaleHint();
  const paper = printForm.elements.paper;
  paper.value = DEF.paper;
  $('#doc-title').textContent = 'Preview / select pages';
  showDocbar(d.name, ` / ${pages} pages${d.size ? ` / original size ${d.size}` : ''}`);
  $('#pagesel').hidden = false;
  $('#thumbs-head').hidden = false;
  thumbsEl.hidden = false;
  renderThumbs(pages, {token: d.token});
  syncThumbs();
  // Document dimensions do not establish which paper is loaded. Keep the default paper size.
  sizeHintLive = false;
  if (kind === 'copy' && d.size === 'A3') {
    // a default flatbed scan is the whole glass (A3-sized): print it on the A4 in the trays, never wait for A3 paper
    setPaperNote('This scan covers the full A3 glass and will shrink to about 70% on A4. To copy an A4 original at actual size, choose A4 when scanning');
  } else {
    sizeHintLive = true;
    refreshSizeHint();
    if (kind === 'upload' && d.size === 'A3') {
      setPaperNote('To print on A3 paper, select A3 manually and confirm that A3 paper is loaded in the tray');
    }
  }
  updatePrint();
}
function setPagesText(text) { pagesInput.value = text; onPagesInput(); }
/** amber note under 纸张 (info: true → grey informational note); null clears it */
function setPaperHint(text, {info = false} = {}) {
  const h = $('#paper-hint');
  h.hidden = !text;
  if (!text) { sizeHintLive = false; return; }
  h.classList.toggle('is-info', info);
  h.querySelector('use').setAttribute('href', info ? '#i-info' : '#i-alert');
  h.querySelector('span').textContent = text;
}
function setPaperNote(text) {
  const n = $('#paper-note');
  n.hidden = !text;
  if (text) n.querySelector('span').textContent = text;
}
function setQualityNote(quality) {
  const n = $('#doc-quality'), text = qualityMessage(quality);
  n.hidden = !text;
  const suffix = quality?.kind === 'office_conversion' ? '' : ' Check the preview or original PDF before printing; printing cannot restore missing content.';
  n.querySelector('span').textContent = text ? text + suffix : '';
}
const selectedPages = () => (P.doc && P.sel.size !== P.doc.pages ? formatRange(P.sel) : '');

const PRINTER_STEPS = ['Check the printer screen; press its power button if the screen is off', 'Wait about one minute after the screen turns on, then select Retry'];
const track = step => {
  const Sx = (cls, ic, label) => `<span class="s ${cls}"><span class="d">${ic ? icon('check') : ''}</span>${label}</span>`;
  return `<div class="track" aria-hidden="true">${Sx('done', 1, 'Sent')}<span class="sep${step > 1 ? ' done' : ''}"></span>${
    step > 1 ? Sx('done', 1, 'Received') : Sx('doing', 0, 'Receiving')}<span class="sep${step > 1 ? ' done' : ''}"></span>${
    step > 1 ? Sx('here', 0, 'Collect pages') : Sx('', 0, 'Collect')}</div>`;
};
const noteHTML = () => P.note ? `<p class="note-warn">${icon('alert')}<span>${esc(P.note)}</span></p>` : '';
const CANCEL = {label: 'Cancel print', icon: 'x', act: 'cancel-print'};
const PRINT_STATES = {
  none: () => null,
  sent: ({msg} = {}) => ({tone: 'busy', icon: 'printer', title: esc(msg || 'Checking print progress...'),
    detail: netDown ? esc(NET_ERR) : 'Large jobs may take longer. This page will show when the printer has received the data.', extra: track(1) + noteHTML(),
    actions: [CANCEL]}),
  blocked: ({msg} = {}) => ({tone: 'warn', icon: 'alert', title: esc(msg),
    detail: 'The queued job will resume after the problem is resolved; do not submit it again.', extra: track(1) + noteHTML(), actions: [CANCEL]}),
  received: ({msg} = {}) => ({tone: 'ok', icon: 'check', title: esc(msg || 'The printer received all data. Check and collect the pages'),
    detail: 'Multiple copies print as complete sets. Select Print again only if another job is needed.', extra: track(2)}),
  warn: ({msg} = {}) => ({tone: 'warn', icon: 'alert', title: esc(msg), extra: noteHTML()}),
  restarted: () => ({tone: 'warn', icon: 'alert', title: 'The station restarted and cannot track this print job',
    detail: 'Check the printer for output before submitting the job again.'}),
  cancelled: ({msg} = {}) => ({tone: 'mute', icon: 'x', title: esc(msg || 'Cancelled')}),
  error: ({msg, steps = null, escalate = true} = {}) => {
    const [head, rest] = splitSentence(stripTrouble(msg) || 'Printing did not finish');
    return {tone: 'err', icon: 'alert-circle', title: `Error: ${esc(head)}`, detail: esc(rest), steps, escalate: escalate && !/administrator/.test(msg || ''),
      actions: P.retry && P.doc ? [{label: 'Retry', icon: 'retry', kind: 'primary', act: 'print'}] : []};
  },
};
let lastPrintBanner = 'none';
/** name: none | sent | blocked | received | warn | restarted | cancelled | error */
function setPrintState(name, data = {}) {
  if (!PRINT_STATES[name]) name = 'none';
  if (name !== P.state) P.note = '';
  P.state = name; P.data = data;
  P.retry = name === 'error' && !!data.retry;
  renderPrintBanner();
  updatePrint();
  if (name !== lastPrintBanner) {
    lastPrintBanner = name;
    if (name !== 'none') revealOnPhone($('#print-status'));
  }
}
function renderPrintBanner() { renderBanner($('#print-status'), PRINT_STATES[P.state](P.data)); }

async function doPrint() {
  if (!P.doc || P.converting || P.rangeError || !P.sel.size || P.busy) return;
  const doc = P.doc, n = P.sel.size, c = copies(), sheets = sheetsPerCopy(n) * c;
  if (PJ.job && PJ.job.running && !confirm('The previous job is still running. Submit another copy?')) return;
  if (sheets > 50 && !confirm(`This job uses about ${sheets} sheets. Continue?`)) return;
  const body = new URLSearchParams({token: doc.token, pages: selectedPages(), paper: printForm.elements.paper.value,
    sides: sides(), copies: String(c), scale: scaleV()});
  P.busy = true; P.lastUse = Date.now();
  if (P.state === 'error') showLocal('none'); else updatePrint();
  const r = await api('/print', {method: 'POST', body});
  P.busy = false;
  if (r.ok && r.data.job) {
    PJ.job = {id: String(r.data.job), running: true};
    PJ.show = true;                              // a new job id takes the banner over
    store.set('print', PJ.job);
    setPrintState('sent', {msg: r.data.desc ? `Sent to printer（${r.data.desc}）` : ''});
    schedulePoll();
  } else if (r.status === 410) {                 // the server already cleaned this document up
    if (P.doc === doc) loadDocument(null);
    showLocal('error', {msg: r.error, escalate: false});
  } else {
    const printerDown = r.status === 503 && /Cannot reach the printer/.test(r.error);
    showLocal('error', {msg: r.error, retry: r.net || r.status === 502 || r.status === 503, steps: printerDown ? PRINTER_STEPS : null, escalate: needsAdmin(r)});
  }
  updatePrint();
}
async function cancelPrint(btn) {
  if (!PJ.job || !PJ.job.running) return;
  if (!confirm('Cancel unsent pages? Pages already received by the printer may still print')) return;
  const id = PJ.job.id;
  btn.disabled = true;
  const r = await api('/cancel', {method: 'POST', body: new URLSearchParams({job: id})});
  if (!PJ.job || PJ.job.id !== id) return;
  if (r.ok) {                                   // terminal: show it, then forget the id (a reload must not bring it back)
    PJ.job = null; store.set('print', null);
    if (PJ.show) setPrintState(PRINT_MAP[r.data.state] || 'cancelled', {msg: String(r.data.msg || '')});
  } else {
    P.note = r.error;
    renderPrintBanner();
  }
}

/* file choose / drag-drop → POST /upload {file} → {token, name, pages, docsize} */
function clearPrintBannerForNewDoc() { showLocal('none'); }
async function acceptFile(file) {
  if (!file) return;
  if (file.size > MAX_MB * 1024 * 1024) {
    showLocal('error', {msg: `File too large (${(file.size / 1048576).toFixed(0)} MB; limit ${MAX_MB} MB). Reduce its size before uploading`, escalate: false});
    return;
  }
  const my = ++docSeq;
  clearPrintBannerForNewDoc();
  setConverting(file.name);
  const body = new FormData();
  body.append('file', file, file.name);
  const r = await api('/upload', {method: 'POST', body});
  if (my !== docSeq) return;                     // a newer pick replaced this one
  if (r.ok && r.data.token) loadDocument({name: r.data.name || file.name, pages: r.data.pages, size: r.data.docsize, token: r.data.token, quality: r.data.quality}, {kind: 'upload'});
  else { loadDocument(null); showLocal('error', {msg: r.error || 'The file upload failed. Retry', escalate: needsAdmin(r)}); }
}
/* a scanned PDF → POST /copy {name} → load it in the print tab (never prints by itself) */
async function copyToPrint(name) {
  if (!name) return;
  const my = ++docSeq;
  setTab('print');
  window.scrollTo({top: 0, behavior: 'smooth'});
  clearPrintBannerForNewDoc();
  setConverting(name, pagesFromName(name) || 8);
  const r = await api('/copy', {method: 'POST', body: new URLSearchParams({name})});
  if (my !== docSeq) return;
  if (r.ok && r.data.token) loadDocument({name: r.data.name || name, pages: r.data.pages, size: r.data.docsize, token: r.data.token, quality: r.data.quality}, {kind: 'copy'});
  else {
    loadDocument(null);
    showLocal('error', {msg: r.error, escalate: needsAdmin(r)});
    if (r.status === 410) refreshFiles();
  }
}
$('#file-input').addEventListener('change', e => { const f = e.target.files && e.target.files[0]; e.target.value = ''; acceptFile(f); });
$('#doc-change').addEventListener('click', () => $('#file-input').click());
const docCard = $('#doc-card');
['dragenter', 'dragover'].forEach(t => docCard.addEventListener(t, e => { e.preventDefault(); docCard.classList.add('is-over'); $('#drop').classList.add('is-over'); }));
['dragleave', 'drop'].forEach(t => docCard.addEventListener(t, e => {
  if (t === 'dragleave' && docCard.contains(e.relatedTarget)) return;
  docCard.classList.remove('is-over'); $('#drop').classList.remove('is-over');
}));
docCard.addEventListener('drop', e => { e.preventDefault(); const f = e.dataTransfer && e.dataTransfer.files[0]; acceptFile(f); });
/* a file dropped anywhere else must not make the browser navigate away from the station */
window.addEventListener('dragover', e => { e.preventDefault(); if (e.dataTransfer && !docCard.contains(e.target)) e.dataTransfer.dropEffect = 'none'; });
window.addEventListener('drop', e => e.preventDefault());

/* ================= shared action buttons (event delegation on data-act) ================= */
document.addEventListener('click', e => {
  const b = e.target.closest('[data-act]');
  if (!b || b.disabled) return;
  const act = b.dataset.act;
  if (act === 'delete') return deleteFile(b);
  if (act === 'scan') return startScan();
  if (act === 'scan-full') {
    scanForm.elements.size.value = 'full';
    sizeTouched = true;
    updateScan();
    return startScan();
  }
  if (act === 'scan-flatbed') {
    scanForm.elements.source.value = 'Flatbed';
    if (!sizeTouched) scanForm.elements.size.value = sizeDefaultFor('Flatbed');
    updateScan();
    return startScan();
  }
  if (act === 'print') return doPrint();
  if (act === 'cancel-print') return cancelPrint(b);
  if (act === 'print-file') return copyToPrint(b.dataset.file);
});
$('#print-go').addEventListener('click', doPrint);

/* ================= GET /status poller =================
   strictly timer-driven: at most one request in flight; next one 1.5 s later while one of MY jobs runs, else 5 s;
   paused while the tab is hidden; a status answer never triggers an immediate re-poll */
let pollTimer = 0, pollDue = 0, polling = false, netFails = 0;
const myJobRunning = () => !!((S.job && S.job.running) || S.legacy || (PJ.job && PJ.job.running) || S.starting);
function schedulePoll(ms) {
  if (document.hidden || polling) return;        // the request in flight schedules the next one when it returns
  const delay = ms != null ? ms : (myJobRunning() ? 1500 : 5000), due = Date.now() + delay;
  if (pollTimer && pollDue <= due) return;       // an earlier poll is already booked
  clearTimeout(pollTimer);
  pollDue = due;
  pollTimer = setTimeout(poll, delay);
}
async function poll() {
  pollTimer = 0;
  if (polling || document.hidden) return;
  polling = true;
  const sj = S.job ? S.job.id : '', pj = PJ.job ? PJ.job.id : '';
  const r = await api('/status?' + new URLSearchParams({scan: sj, print: pj}));
  polling = false;
  if (r.ok) applyStatus(r.data, sj, pj);
  else if (r.net || r.status >= 500) onPollFail();
  schedulePoll();
}
function onPollFail() {
  if (++netFails < 2 || netDown) return;         // one blip is not an outage
  netDown = true;
  setDeviceChip('scanner', 'err', 'Unreachable', NET_ERR);
  setDeviceChip('printer', 'err', 'Unreachable', NET_ERR);
  renderScan();
  if (P.state === 'sent') renderPrintBanner();
  updatePrint();
}
const PRINT_MAP = {running: 'sent', blocked: 'blocked', done: 'received', slow: 'warn', unknown: 'warn', error: 'error', cancelled: 'cancelled'};
/* the station was updated while this page stayed open: old pages misread new states (2026-10-19: a 'review'
   scan showed as a red error in tabs opened before the update). Reload as soon as nothing is in flight: no scan
   or print request out, no scan running, no document touched in the last RETAIN minutes (the station has deleted
   it after that). What is on screen comes back once after the reload; a running print job resumes from 'print'.
   Returns true when the page is reloading. */
function checkBuild(build) {
  if (!build || !BOOT.build || build === BOOT.build) return false;
  return reloadForUpdate();
}
/* the reload itself, also used when /scan refuses this page as another build; false = not now */
function reloadForUpdate() {
  if (S.starting || S.view === 'busy' || S.legacy || P.busy || P.converting) return false;
  if (P.doc && Date.now() - P.lastUse < RETAIN * 60000) return false;
  store.set('reload', {
    scan: ['idle', 'busy'].indexOf(S.view) < 0 ? {view: S.view, data: S.data} : null,
    print: !PJ.job && PJ.show && P.state !== 'none' ? {state: P.state, data: P.data} : null,
  });
  saveScanForm();
  location.reload();
  return true;
}
function applyStatus(d, sj, pj) {
  if (checkBuild(d.build)) return;               // nothing below may change the saved state
  /* my job ids changed while this request was out (a scan/print just started): the answer is stale — ignore it,
     the next timed poll asks with the new ids */
  if (S.starting || (S.job ? S.job.id : '') !== sj || (PJ.job ? PJ.job.id : '') !== pj) return;
  const wasDown = netDown;
  netDown = false; netFails = 0;
  const pr = d.printer || {};
  if (pr.online === true) setDeviceChip('printer', 'ok', 'Online', pr.ip ? `Printer ${pr.ip}` : '');
  else if (pr.online === false) setDeviceChip('printer', 'err', 'Unreachable', pr.ip ? `Printer address ${pr.ip}` : '');
  else setDeviceChip('printer', 'idle', 'Checking...');
  /* older station software answers only {busy, msg}: read the scanner from those */
  const sc = d.scanner || {busy: !!d.busy, pages: 0, mine: false, phase: '', msg: typeof d.msg === 'string' ? d.msg : ''};
  const mine = !!(sc.busy && (sc.mine || S.legacy));
  if (sc.busy && mine) setDeviceChip('scanner', 'busy', 'Scanning');
  else if (sc.busy) setDeviceChip('scanner', 'wait', 'In use', sc.msg || '');
  else setDeviceChip('scanner', 'ok', 'Idle');
  /* someone else holds the scanner; a 409 answer I got keeps its wording as the title until the scanner is free */
  S.others = sc.busy && !mine ? {msg: String(sc.msg || ''), pages: sc.pages || 0, title: (S.others && S.others.title) || ''} : null;

  /* my scan */
  if (S.legacy) {                                // started without a job id: follow the scanner lock
    if (sc.busy) { S.view = 'busy'; S.data = {msg: String(sc.msg || 'Scan started'), pages: sc.pages || 0, phase: sc.phase || 'scanning'}; }
    else { S.legacy = false; S.view = 'ended'; S.data = {msg: d.scanner ? '' : String(d.msg || '')}; refreshFiles(); }
  } else if (sj && S.job && S.job.id === sj) {
    const j = d.scan;
    if (!j) {                                    // the server doesn't know my job any more
      S.view = 'restarted'; S.data = {msg: RESTART_SCAN_MSG}; refreshFiles();
      S.job = null; store.set('scan', null);
    } else if (j.state === 'running') {
      S.view = 'busy';
      S.data = {msg: String(j.msg || ''), pages: sc.mine ? (sc.pages || 0) : 0, phase: sc.mine ? sc.phase : ''};
    } else {                                     // terminal: show it once, then forget the id (a reload must not resurrect it)
      S.view = ['done', 'review', 'partial', 'error'].indexOf(j.state) >= 0 ? j.state : 'error';
      S.data = {msg: String(j.msg || ''), file: String(j.file || '')};
      store.set('scan-review', S.view === 'review' ? S.data : null);
      if (S.data.file) freshName = S.data.file;
      refreshFiles();
      S.job = null; store.set('scan', null);
    }
  }
  renderScan();

  /* my print job — its state is shown only while it owns the print banner (PJ.show) */
  if (pj && PJ.job && PJ.job.id === pj) {
    const j = d.print;
    if (!j) {
      if (PJ.show) setPrintState('restarted');
      PJ.job = null; store.set('print', null);
    } else {
      const name = PRINT_MAP[j.state] || 'warn';
      const running = j.state === 'running' || j.state === 'blocked';
      if (PJ.show && (P.state !== name || (P.data.msg || '') !== (j.msg || '') || wasDown)) setPrintState(name, {msg: String(j.msg || '')});
      if (!running) { PJ.job = null; store.set('print', null); }
    }
  } else if (wasDown) renderPrintBanner();
  updatePrint();
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { clearTimeout(pollTimer); pollTimer = 0; }
  else { schedulePoll(0); refreshFiles(); }
});
/* the file list's "还剩 N 分钟" countdown: refresh every 30 s while the page is visible */
setInterval(() => { if (!document.hidden) refreshFiles(); }, 30000);

/* ================= start ================= */
/* Running jobs resume polling; review results remain visible while their saved PDF exists. */
S.job = (j => j && typeof j.id === 'string' && j.running ? {id: j.id, running: true} : null)(store.get('scan'));
PJ.job = (j => j && typeof j.id === 'string' && j.running ? {id: j.id, running: true} : null)(store.get('print'));
PJ.show = !!PJ.job;
setFiles(BOOT.files || [], {fresh: null});
restoreScanForm(store.get('scan-form'));
const savedScanReview = store.get('scan-review');
if (!S.job && savedScanReview && typeof savedScanReview.file === 'string' && files.some(f => f.name === savedScanReview.file)) {
  S.view = 'review'; S.data = {file: savedScanReview.file, msg: String(savedScanReview.msg || '')};
} else if (!S.job) store.set('scan-review', null);
/* the screen as it was before an update reload (checkBuild), shown once */
const reloaded = store.get('reload') || {};
store.set('reload', null);
const rs = reloaded.scan;
if (!S.job && S.view === 'idle' && rs && ['done', 'review', 'partial', 'error', 'restarted', 'ended'].indexOf(rs.view) >= 0) {
  const file = rs.data && typeof rs.data.file === 'string' ? rs.data.file : '';
  if (!file || files.some(f => f.name === file)) { S.view = rs.view; S.data = {msg: String((rs.data && rs.data.msg) || ''), file}; }
}
if (S.job && S.job.running) { S.view = 'busy'; S.data = {msg: '', pages: 0, phase: 'scanning'}; }
renderScan();
loadDocument(null);
setRadio(printForm, 'scale', 'auto'); updateScaleHint();
onCopies();
const rp = reloaded.print;
if (PJ.job) setPrintState('sent', {msg: ''});
else if (rp && Object.prototype.hasOwnProperty.call(PRINT_STATES, rp.state)) setPrintState(rp.state, rp.data && typeof rp.data === 'object' ? rp.data : {});
else setPrintState('none');
setTab(store.get('tab') || 'scan');
poll();

/* UI hooks (debugging / automation) */
window.Station = {setTab, setScanState, setFiles, loadDocument, setConverting, setPrintState, setPaperHint, setPagesText,
  setDeviceChip, parseRange, formatRange, selectedPages, refreshFiles};
})();
</script>
</body>
</html>'''  # 页面(单文件部署)：样式/脚本/图标全部内联，不连外网


def _file_rows():
    """Scanned PDFs 列表。文件随时可能被清理/删除，逐个容错——不能因为一个文件消失让整页 500。"""
    rows, now = [], time.time()
    for p in glob.glob(os.path.join(SCANS, "scan-*.pdf")):
        try:
            mt, sz = os.path.getmtime(p), os.path.getsize(p)
        except OSError:
            continue
        rows.append({"name": os.path.basename(p), "kb": max(1, sz // 1024), "mt": mt,
                     "quality": _read_scan_quality(p), "raw": os.path.isdir(_raw_dir(os.path.basename(p))),
                     "small": os.path.exists(_small_path(os.path.basename(p))),
                     "left_min": max(0, int((RETAIN_SEC - (now - mt)) // 60))})
    rows.sort(key=lambda r: r["mt"], reverse=True)
    for r in rows:
        del r["mt"]
    return rows


def _boot_json():
    """页面初始化数据(选项 + 当前文件列表)。放进 <script> 里，所以要挡住 </script> 注入。"""
    boot = {
        "sources": list(SOURCES.items()), "sizes": list(SCAN_SIZES.items()), "res": RES,
        "res_default": "300", "modes": list(MODES.items()), "papers": list(PAPERS.items()),
        "sides": list(SIDES.items()), "scales": list(SCALES.items()), "files": _file_rows(),
        "retain_min": RETAIN_SEC // 60, "max_upload_mb": MAX_UPLOAD // 1024 // 1024,
        "adf_max_w": int(ADF_MAX_W), "build": BUILD_ID,
        # feeder room beside the paper per size (mm per side), so the page's hints follow the real window
        "adf_side_room": {n: (adf_window(w, h)[2] - w) / 2 for n, w, h in STD_SIZES if n in SCAN_SIZES},
    }
    return json.dumps(boot, ensure_ascii=False).replace("<", "\\u003c")


@app.route("/")
def index():
    return Response(PAGE.replace("__BOOT_JSON__", _boot_json()), mimetype="text/html")


@app.route("/files")
def files():
    return jsonify(files=_file_rows())


def _scan_worker(jid, args):
    try:
        run_scan(jid, *args)
    finally:
        scan_lock.release()                      # 锁在请求里拿、在这里放：只有真正开扫的请求才会回 202


@app.route("/scan", methods=["POST"])
def scan():
    f = request.form
    # A page from another build is refused: tabs opened before the 2026-10-19 update still post the old checked-by-
    # default darkening/rotation and cannot read the new result states. Their error card shows this text.
    if f.get("build") != BUILD_ID:
        return jsonify(error="The station was updated. Refresh this page before continuing", stale=True), 400
    src, res = f.get("source", "Flatbed"), f.get("res", "300")
    mode, size = f.get("mode", "Color"), f.get("size", "full")
    if src not in SOURCES or res not in RES or mode not in MODES or size not in SCAN_SIZES:
        return jsonify(error="Invalid scan settings. Refresh the page and retry"), 400
    if not DEV:
        return jsonify(error="Scanner is not configured. Set STATION_DEV before scanning."), 503
    # ⚠️ 早期先"非阻塞拿锁→马上放掉"判忙，再让工作线程重新拿锁：两人同时点时两个都回 202，
    # 抢输的那个线程静默放弃，它的浏览器随后把别人的扫描件当成自己的。现在锁在请求里就拿到并交给线程。
    if not scan_lock.acquire(blocking=False):
        return jsonify(error="Another user is scanning. Wait until they finish"), 409
    try:
        jid = new_job("scan")
        scanner.update(job=jid, pages=0)
        args = (src, res, mode, size, f.get("blank") == "1", f.get("ocr") == "1",
                f.get("enh") == "1", f.get("deskew") == "1", f.get("paper_white") == "1")
        threading.Thread(target=_scan_worker, args=(jid, args), daemon=True).start()
    except Exception:
        scan_lock.release()
        raise
    return jsonify(job=jid), 202


@app.route("/status")
def st():
    """扫描仪忙不忙(给所有人) + 调用者自己那两个作业的状态(只给发起人)。
    busy/msg/pmsg 三个旧字段只给升级前就开着的旧页面用：让它们提示"请刷新"，而不是显示 undefined 或假状态。"""
    busy = scan_lock.locked()
    converting = office_lock.locked()
    sj = request.args.get("scan", "")
    pj = request.args.get("print", "")
    mine = busy and scanner.get("job") == sj
    if not busy:
        generic = "Ready"
    elif scanner.get("phase") == "processing":
        generic = "Another scan is being processed (%d pages). The scanner will be available afterward" % scanner.get("pages", 0)
    else:
        generic = "Scanner in use (%d pages scanned)" % scanner["pages"] if scanner.get("pages") else "Scanner in use"
    return jsonify(printer=printer_status(), scanner={"busy": busy, "pages": scanner.get("pages", 0), "mine": mine,
                            "phase": scanner.get("phase", ""), "msg": generic},
                   scan=job_view(sj, "scan"), print=job_view(pj, "print"),
                   busy=busy or converting, conversion={"busy": converting}, build=BUILD_ID, msg="The station was updated. Refresh this page before continuing",
                   pmsg="The station was updated. Refresh this page before continuing")


# ---- 打印：页数 / 逐页预览 / 页码选择 ----
SIDES_IPP = {"None": "one-sided", "DuplexNoTumble": "two-sided-long-edge",
             "DuplexTumble": "two-sided-short-edge"}
_pv_sem = threading.BoundedSemaphore(2)          # 同时最多渲 2 页，别压垮 NAS；不同文档互不排队
_pv_locks = {}
_pv_locks_guard = threading.Lock()
TOKEN_RE = re.compile(r"[0-9a-f]{32}")


def pdf_info(pdf):
    """(页数, 文档尺寸标签)。尺寸取第一页，吸附不到标准尺寸就如实给毫米数。
    pdfinfo 失败(加密/损坏)必须报错——早期忽略返回码，坏文件被当成"1 页"继续往下走。"""
    info = subprocess.run(["pdfinfo", pdf], capture_output=True, text=True, timeout=30)
    if info.returncode != 0:
        raise RuntimeError((info.stderr or "may not be a pdf").strip()[:200])
    pages, docsize = 0, ""
    for line in info.stdout.splitlines():
        if line.startswith("Pages:"):
            pages = int(line.split()[1])
        elif line.startswith("Page size:"):        # 例: "Page size: 841.89 x 1190.55 pts (A3)"
            try:
                parts = line.split(":")[1].split()
                wmm, hmm = float(parts[0]) / 72 * 25.4, float(parts[2]) / 72 * 25.4
                sn = snap_size(wmm, hmm, tol=6.0)
                docsize = sn[0] if sn else "%.0f×%.0fmm" % (wmm, hmm)
            except Exception:
                pass
    if pages < 1:
        raise RuntimeError("may not be a pdf (0 pages)")
    return pages, docsize


def job_page_count(d):
    """作业目录里 doc.pdf 的页数（上传时记在 pages 文件里，丢了就现数）。"""
    try:
        with open(os.path.join(d, "pages")) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return pdf_info(os.path.join(d, "doc.pdf"))[0]


def _touch(d):
    """打印作业目录"还在用"：清理按最后访问时间算，用户还在选页时不能被删。"""
    try:
        os.utime(d)
    except OSError:
        pass


def render_preview(d, n, hi=False):
    """第 n 页(从 1 起)的预览图：按需渲染 + 缓存。整份文档每一页都能看。
    hi=True 是点开看大图用的 120dpi 版本(60dpi 缩略图放大后看不清字)。"""
    out = os.path.join(d, "pg-%d%s.png" % (n, "-hi" if hi else ""))
    if os.path.exists(out):
        return out
    with _pv_locks_guard:
        lk = _pv_locks.setdefault(out, threading.Lock())
    with lk:
        if not os.path.exists(out):
            with _pv_sem:
                tmp = os.path.join(d, "tmp-%d%s" % (n, "-hi" if hi else ""))
                subprocess.run(["pdftoppm", "-png", "-r", "120" if hi else "60", "-f", str(n), "-l", str(n),
                                "-singlefile", os.path.join(d, "doc.pdf"), tmp],
                               check=True, timeout=60, capture_output=True)
                os.replace(tmp + ".png", out)
    with _pv_locks_guard:
        _pv_locks.pop(out, None)
    return out


def parse_page_ranges(spec, total):
    """'1-3,5 8到10' → [1,2,3,5,8,9,10]。留空/全部 = 整份。看不懂或超范围抛 ValueError(人话)。
    结果总是升序去重——打印机只会按文档顺序出纸，不假装支持乱序/重复。
    和前端 parseSpec 保持同一套写法(全角数字、中文逗号/到/至、all 都认)。"""
    s = (spec or "").strip()
    if len(s) > 4000:                   # 只防滥用：200 页隔页选的规范写法就有 ~350 字符，不能卡正常用法
        raise ValueError("Page selection is too long. Use a compact range such as 1-3,5,8-10")
    s = s.translate({0xFF10 + i: 0x30 + i for i in range(10)})          # 全角数字 → 半角
    for a, b in (("，", ","), ("、", ","), ("；", ","), (";", ","), ("－", "-"), ("—", "-"),
                 ("–", "-"), ("~", "-"), ("～", "-"), ("到", "-"), ("至", "-"),
                 ("第", ""), ("页", "")):
        s = s.replace(a, b)
    s = re.sub(r"\s*-\s*", "-", s)
    s = re.sub(r"[\s,]+", ",", s).strip(",")
    if s.lower() in ("", "全部", "all"):
        return list(range(1, total + 1))
    picked = set()
    for part in s.split(","):
        m = re.fullmatch(r"(\d{1,5})(?:-(\d{1,5}))?", part)
        if not m:
            raise ValueError("Invalid page selection %s. Use a range such as 1-3,5,8-10" % part[:20])
        a = int(m.group(1)); b = int(m.group(2) or a)
        if a > b:
            a, b = b, a
        if a < 1 or b > total:
            raise ValueError("This file has %d pages; selection %s is out of range" % (total, part))
        picked.update(range(a, b + 1))
    return sorted(picked)


def compress_ranges(pages):
    """[1,2,3,5] → '1-3,5'（给 lp 的 page-ranges 和给人看的描述共用一个写法）。"""
    out, i = [], 0
    while i < len(pages):
        j = i
        while j + 1 < len(pages) and pages[j + 1] == pages[j] + 1:
            j += 1
        out.append(str(pages[i]) if i == j else "%d-%d" % (pages[i], pages[j]))
        i = j + 1
    return ",".join(out)


def _new_print_doc():
    token = uuid.uuid4().hex
    d = os.path.join(JOBS, token)
    os.makedirs(d, exist_ok=True)
    return token, d


def _finish_print_doc(d):
    """doc.pdf 就位后：数页数、记下来、先渲Page 1(坏 PDF 在这里就报错)。返回 (页数, 尺寸)。"""
    pages, docsize = pdf_info(os.path.join(d, "doc.pdf"))
    with open(os.path.join(d, "pages"), "w") as fh:
        fh.write(str(pages))
    render_preview(d, 1)
    return pages, docsize


IMG_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp")
MAX_IMAGE_PIXELS = 60_000_000       # 6000 万像素(A3 600dpi 约 7000 万的 85%)：再大就是"解压炸弹"，几百 KB 能撑爆内存
# 只认这些真实格式(按文件内容判断，不看扩展名)。⚠️ 内容是 EPS/PS 却改名成 .webp/.png 的文件，
# Pillow 会去调 Ghostscript 解析、而且没有超时——在 1 号进程里就是一个能把整站卡死的口子。
IMG_FORMATS = ["PNG", "JPEG", "MPO", "WEBP", "BMP", "GIF", "TIFF"]


def _img_formats():
    from PIL import Image
    Image.init()                                # 先注册全部插件，否则 MPO/WEBP/TIFF 可能还不在表里
    return [f for f in IMG_FORMATS if f in Image.OPEN]


def _check_image(src):
    """只读文件头：确认真实格式在白名单里、像素数不超限。返回 (宽, 高)；不合格抛 ValueError(人话)。"""
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(src, formats=_img_formats()) as im:
            w, h = im.size
    except UnidentifiedImageError:
        raise ValueError("Cannot open this image. Choose another file or export it as JPG or PNG")
    if w * h > MAX_IMAGE_PIXELS:
        raise ValueError("Image too large (%d x %d pixels). Resize it before uploading" % (w, h))
    return w, h


def _image_to_png(src, dst):
    """用 Pillow 把 img2pdf 不认的图(webp、带透明的 TIFF、16 位 RGBA…)转成普通 PNG。
    先只读文件头看尺寸，超过 MAX_IMAGE_PIXELS 直接拒绝——绝不在 1 号进程里解码一张炸弹图。"""
    from PIL import Image
    _check_image(src)
    with Image.open(src, formats=_img_formats()) as im:
        if im.mode in ("I;16", "I", "I;16B", "I;16L"):
            out = im.point(lambda v: v / 256).convert("L")
        else:
            out = im.convert("RGBA" if "A" in im.getbands() else ("L" if im.mode in ("1", "L") else "RGB"))
        out.save(dst)
    return dst
OFFICE_EXT = (".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
              ".odt", ".ods", ".odp", ".rtf", ".txt", ".csv", ".wps", ".et", ".dps")


@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="No file received. Choose the file again"), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext in (".heic", ".heif"):
        return jsonify(error="iPhone HEIC photos are unsupported. Export the photo as JPG, or use the camera Most Compatible format"), 400
    if ext not in (".pdf",) + IMG_EXT + OFFICE_EXT:
        return jsonify(error="Unsupported file type. Use PDF, images (JPG/PNG), Word, Excel, PowerPoint or WPS"), 400
    is_office = ext in OFFICE_EXT
    if is_office and not office_lock.acquire(blocking=False):
        return jsonify(error="Another Office file is being converted. Try uploading again shortly"), 429
    d = None
    conversion = None
    quality = None
    try:
        token, d = _new_print_doc()
    except Exception:
        if is_office:
            office_lock.release()
        raise
    src = os.path.join(d, "src" + ext)
    pdf = os.path.join(d, "doc.pdf")
    try:
        f.save(src)
        if ext == ".pdf":
            shutil.copy(src, pdf)
        elif ext in IMG_EXT:
            _check_image(src)                           # 真实格式 + 像素数，先看文件头再说
            if ext == ".webp":                          # img2pdf 不认 webp → 先用 Pillow 转成 PNG
                src = _image_to_png(src, os.path.join(d, "conv.png"))
            # 排进 A4 页面(等比不裁)，页面方向跟着图片走(横图出横版页，不再缩成竖版里的一小条)，
            # 四周留 5mm——按实际大小打印时整张图都在打印机可打印区(边缘 4.2mm 打不到)里。
            # -r ifvalid：安卓照片常带非法 EXIF 方向值，默认 auto 会直接报错整张传不上。
            i2p = ["--pagesize", "A4", "--auto-orient", "--fit", "into",
                   "--border", "5mm", "--rotation", "ifvalid", "-o", pdf]
            r = run_group(["img2pdf", src] + i2p, timeout=60)          # 超时整组杀，不留孤儿
            if r.returncode != 0:
                # img2pdf 0.4.4 不认带透明的 TIFF、16 位 RGBA 等：转成普通 PNG 再来一次，而不是让用户白试
                src = _image_to_png(src, os.path.join(d, "conv.png"))
                r = run_group(["img2pdf", src] + i2p, timeout=60)
                if r.returncode != 0:
                    raise RuntimeError("cannot identify image: %s" % (r.stderr or "").strip()[-150:])
        else:
            conversion = office_conversion.convert_office(src, pdf, run_group, pdf_info, log_event)
        pages, docsize = _finish_print_doc(d)
        if conversion is not None:
            quality = office_conversion.page_quality(conversion["source_pages_hint"], pages, conversion["layout_risks"])
            conversion["quality"] = quality
            with open(os.path.join(d, "conversion.json"), "w", encoding="utf-8") as fh:
                json.dump(conversion, fh, ensure_ascii=False)
    except subprocess.TimeoutExpired:
        shutil.rmtree(d, ignore_errors=True)
        log_event("upload-timeout", f.filename)
        return jsonify(error="Conversion timed out. Export a PDF from the original application and upload it"), 422
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        detail = getattr(e, "stderr", b"") or b""
        detail = detail.decode("utf-8", "replace") if isinstance(detail, bytes) else str(detail)
        log_event("upload-fail", "%s %s %s" % (f.filename, e, detail[:200]))
        if ext in OFFICE_EXT:                        # Office 转换的任何失败都给同一句能照做的话；原因进日志
            return jsonify(error="Conversion failed. The file may be damaged or unsupported. Export a PDF from the original application and upload it"), 422
        return jsonify(error=friendly("%s %s" % (e, detail))), 422
    finally:
        if is_office:
            office_lock.release()
    return jsonify(token=token, name=f.filename, pages=pages, docsize=docsize, quality=quality)


@app.route("/preview/<token>")
def preview(token):
    if not TOKEN_RE.fullmatch(token):
        return "bad", 400
    d = os.path.join(JOBS, token)
    if not os.path.exists(os.path.join(d, "doc.pdf")):
        return "no preview", 404
    try:
        n = int(request.args.get("p", "0")) + 1         # p 从 0 起(沿用旧接口)，页码从 1 起
    except ValueError:
        n = 1
    try:
        if not (1 <= n <= job_page_count(d)):
            return "no such page", 404
        _touch(d)
        hi = request.args.get("hi") == "1"
        return send_from_directory(d, os.path.basename(render_preview(d, n, hi)), max_age=3600)
    except (FileNotFoundError, NotFound):
        return "no preview", 404                        # 目录刚被清理/删掉
    except Exception as e:
        if not os.path.exists(os.path.join(d, "doc.pdf")):
            return "no preview", 404                    # 渲染/数页前目录就被清理掉了——不是站点故障
        log_event("preview-fail", "%s p%d %s" % (token[:8], n, e))
        return "preview failed", 500


SCALES = {"auto": "Actual size (shrink only if needed)", "fit": "Fit printable area (include page edges)"}


@app.route("/print", methods=["POST"])
def do_print():
    token = request.form.get("token", "")
    if not TOKEN_RE.fullmatch(token):
        return jsonify(error="This file is no longer available. Choose it again"), 400
    d = os.path.join(JOBS, token)
    pdf = os.path.join(d, "doc.pdf")
    if not os.path.exists(pdf):
        return jsonify(error="This file expired and was removed. Choose it again"), 410
    paper = request.form.get("paper", "A4")
    sides = request.form.get("sides", "None")
    scale = request.form.get("scale", "auto")
    copies = (request.form.get("copies") or "1").strip()
    if paper not in PAPERS or sides not in SIDES or scale not in SCALES:
        return jsonify(error="Invalid print settings. Refresh the page and retry"), 400
    # 份数由用户手输：卡死在 1..200，挡住 0/负数/小数/超大值(打印机是真出纸的，错了很浪费)
    if not (copies.isascii() and copies.isdigit()) or not (1 <= int(copies) <= 200):   # isdigit 认 "²"
        return jsonify(error="Copies must be an integer from 1 to 200"), 400
    copies = str(int(copies))
    _touch(d)
    # 打哪几页：留空 = 全部。页码按这份文档的真实页数校验，超范围直接告诉用户，绝不静默少打
    try:
        total = job_page_count(d)
        sel = parse_page_ranges(request.form.get("pages", ""), total)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        return jsonify(error=friendly(e)), 422
    ranges = compress_ranges(sel)
    duplex = sides != "None"
    sheets = ((len(sel) + 1) // 2 if duplex else len(sel)) * int(copies)
    # 分辨率：队列 PPD(Generic PostScript)固定 600dpi、没有可选项，传 Resolution/print-quality 都是空操作，已删。
    # 缩放：⚠️ 以前固定 fit-to-page——2026-09-23 过真实 CUPS 链实测：同尺寸 A4 被缩到 96%，A5 被放大 136%。
    # print-scaling=auto = 放得下就 1:1、放不下(A3/Letter→A4)才缩小；fit = 整页缩进可打印区(纸边内容全保留，约缩 4%)。
    # 双面同时给 PPD 写法(Duplex=)和 IPP 标准写法(sides=)。多份一律逐份(collate)：不逐份时打印机自己
    # 复制每一页(1 1 2 2…)，双面时同一页的两份落到一张纸正反面——2026-09-21 用户报"有些双面有些不双面"的根因。
    cmd = ["lp", "-d", QUEUE, "-n", copies,
           "-o", "PageSize=%s" % paper, "-o", "Duplex=%s" % sides, "-o", "sides=%s" % SIDES_IPP[sides],
           "-o", "print-scaling=%s" % scale]
    if int(copies) > 1:
        cmd += ["-o", "collate=true"]
    if len(sel) < total:
        cmd += ["-o", "page-ranges=" + ranges]
    cmd.append(pdf)
    try:
        qst = subprocess.run(["lpstat", "-p", QUEUE], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        qst = ""
    if "disabled" in qst.lower():                   # 队列被停用：作业只会一直排着，不会出纸
        log_event("print-queue-disabled", qst.strip()[:200])
        return jsonify(error="The print queue is disabled. Contact the administrator to enable queue %s" % QUEUE), 503
    online, ip = printer_online()
    _pstate.update(online=online, ip=ip, t=time.time())
    if not online:
        log_event("print-offline", ip)
        return jsonify(error="Cannot reach the printer (%s). Check its power, sleep mode and network cable, then retry" % ip), 503
    which = ("All %d pages" % total) if len(sel) == total else ("Pages %s (%d pages total, %d selected)" % (ranges, total, len(sel)))
    desc = "%s copies / %s / %s / %s" % (copies, paper, SIDES[sides], which)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as e:
        log_event("print-error", e)
        return jsonify(error=friendly(e)), 502
    if r.returncode != 0:
        log_event("print-fail", r.stderr)
        return jsonify(error="Print submission failed: " + friendly((r.stderr or "").strip())), 502
    # 从 "request id is M437-25 (1 file(s))" 里取作业号，然后盯真实结局
    m = re.search(r"request id is (\S+)", r.stdout or "")
    cups_id = m.group(1) if m else ""
    pjid = new_job("print", cups=cups_id)
    # 连 lp 的实际选项一起落盘：双面/页码这类"时好时坏"的问题，事后要能对上每个作业到底发了什么
    log_event("print-submit", "%s %s | %s" % (cups_id, desc, " ".join(cmd[3:-1])))
    if cups_id:
        job_update(pjid, state="running", msg="Sent to the printer (%s); printing..." % desc)
        threading.Thread(target=watch_print_job, args=(pjid, cups_id, desc, sheets), daemon=True).start()
    else:
        job_update(pjid, state="unknown", msg="Submitted (%s), but no job ID was returned. Check the printer for output" % desc)
    return jsonify(ok=True, job=pjid, desc=desc, sheets=sheets)


@app.route("/cancel", methods=["POST"])
def cancel_print():
    """取消自己刚提交的打印作业(按页面拿到的作业 id，不能取消别人的)。
    ⚠️ 队列是 raw 9100：已经送进打印机内存的页照样会打出来，只能拦住还没送出去的部分——文案要说实话。"""
    pjid = request.form.get("job", "")
    with _jobs_lock:
        j = dict(_jobs.get(pjid) or {})
    if j.get("kind") != "print" or not j.get("cups"):
        return jsonify(error="Print job not found; it may already have finished"), 404
    if j.get("state") in ("done", "error", "cancelled"):
        return jsonify(error="This job has already ended and cannot be cancelled"), 409
    r = subprocess.run(["cancel", j["cups"]], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        log_event("print-cancel-fail", "%s %s" % (j["cups"], r.stderr))
        return jsonify(error="Cancellation failed: " + friendly(r.stderr)), 502
    msg = "Cancelled. Unsent pages will not print; pages already received by the printer may still emerge"
    job_update(pjid, state="cancelled", msg=msg)
    log_event("print-cancel", j["cups"])
    return jsonify(ok=True, state="cancelled", msg=msg)


def _scan_name_ok(name):
    return bool(re.fullmatch(r"scan-[0-9]{8}-[0-9]{6}-[0-9]+p\.pdf", name or ""))


@app.route("/delete", methods=["POST"])
def delete():
    name = request.form.get("name", "")
    if not _scan_name_ok(name):
        return jsonify(error="Invalid filename"), 400
    try:
        os.remove(os.path.join(SCANS, name))
    except FileNotFoundError:
        pass                                        # 已经被清理了——结果一样
    except OSError as e:
        return jsonify(error=friendly(e)), 500
    # Deleting a scan deletes its check record, e-mail copy and kept originals too (a deleted contract must not linger)
    try:
        os.remove(os.path.join(SCANS, name + ".quality.json"))
    except OSError:
        pass
    shutil.rmtree(_raw_dir(name), ignore_errors=True)
    try:
        os.remove(_small_path(name))
    except OSError:
        pass
    log_event("scan-delete", name)
    return jsonify(ok=True)


@app.route("/small/<path:name>")
def small_copy(name):
    """The e-mail sized copy (JPEG q90, about a third of the size). The main PDF is the scanner's lossless output."""
    if not _scan_name_ok(name) or not os.path.exists(_small_path(name)):
        return "not found", 404
    return send_from_directory(SCANS, ".small-" + name, as_attachment=True,
                               download_name=name[:-4] + "-small.pdf")


@app.route("/raw/<path:name>")
def raw_original(name):
    """Kept originals of a processed scan: /raw/scan-...pdf = the untouched frames as a lossless PDF,
    /raw/scan-...json = the processing record."""
    pdf = name[:-5] + ".pdf" if name.endswith(".json") else name
    if not _scan_name_ok(pdf):
        return "not found", 404
    d = _raw_dir(pdf)
    if not os.path.isdir(d):
        return jsonify(error="The original scan was removed after its %d-minute retention period" % (RETAIN_SEC // 60)), 404
    if name.endswith(".json"):
        return send_from_directory(d, "record.json", as_attachment=False, mimetype="application/json")
    out = os.path.join(d, "original.pdf")
    if not os.path.exists(out):
        frames = sorted(glob.glob(os.path.join(d, "p*.png")))
        if not frames:
            return "not found", 404
        tmp = out + ".tmp"
        try:
            with open(tmp, "wb") as fh:
                subprocess.run(["img2pdf"] + frames, stdout=fh, check=True, timeout=300)
            os.replace(tmp, out)
        except Exception as exc:
            log_event("scan-raw-pdf-fail", "%s: %s" % (type(exc).__name__, str(exc)[:150]))
            return jsonify(error="Could not generate the original scan PDF. Retry"), 500
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return send_from_directory(d, "original.pdf", as_attachment=True,
                               download_name=pdf[:-4] + "-original.pdf")


@app.route("/f/<path:name>")
def dl(name):
    # 只允许取扫描出的 PDF，挡目录穿越
    if not _scan_name_ok(name):
        return "not found", 404
    return send_from_directory(SCANS, name, as_attachment=False)


@app.route("/copy", methods=["POST"])
def copy_to_print():
    """把New scan的 PDF 送进打印流程（复印）：不直接打，而是载入打印页让用户选纸张/份数并预览，
    避免误触真出纸。返回 docsize——Full area扫出来的是 A3 页面，前端据此提示/选纸，不再静默缩印。"""
    name = request.form.get("name", "")
    if not _scan_name_ok(name):
        return jsonify(error="Invalid filename"), 400
    src = os.path.join(SCANS, name)
    quality = _read_scan_quality(src)
    token, d = _new_print_doc()
    try:
        shutil.copy(src, os.path.join(d, "doc.pdf"))
    except FileNotFoundError:
        shutil.rmtree(d, ignore_errors=True)
        return jsonify(error="This file was removed after %d minutes. Scan it again" % (RETAIN_SEC // 60)), 410
    try:
        pages, docsize = _finish_print_doc(d)
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        log_event("copy-fail", "%s %s" % (name, e))
        return jsonify(error=friendly(e)), 422
    return jsonify(token=token, name=name, pages=pages, docsize=docsize, quality=quality)


@app.route("/events.log")
def events():
    """事件日志下载(排障用)。"""
    if not os.path.exists(EVENTS):
        return "No events recorded yet\n", 200, {"Content-Type": "text/plain; charset=utf-8"}
    return send_from_directory(SCANS, "events.log", mimetype="text/plain")


@app.errorhandler(413)
def too_large(e):
    return jsonify(error="File too large (limit %d MB). Reduce its size before uploading" % (MAX_UPLOAD // 1024 // 1024)), 413


@app.errorhandler(500)
def server_error(e):
    # 前端都按 JSON 解析——返回 Flask 默认的 HTML 500 会被误报成"连不上打印站"
    log_event("http-500", "%s %s" % (request.path, getattr(e, "original_exception", e)))
    return jsonify(error="Station error. Retry, or contact the administrator if it persists"), 500


if __name__ == "__main__":
    threading.Thread(target=recover_orphan_workdirs, daemon=True).start()   # 不挡住开门：页面先能用
    threading.Thread(target=cleaner, daemon=True).start()
    threading.Thread(target=reap_orphans, daemon=True).start()
    app.run(host="0.0.0.0", port=8071, threaded=True)
