"""User-visible safeguards for scans whose geometry or orientation needs review."""

import ast
import json
from pathlib import Path
import shutil
import subprocess

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "app/scan_app.py"


@pytest.fixture(scope="module")
def page():
    tree = ast.parse(SOURCE.read_text())
    return next(ast.literal_eval(n.value) for n in tree.body
                if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PAGE" for t in n.targets))


def section(page, start, end):
    return page[page.index(start):page.index(end, page.index(start))]


def run_js(source):
    if not shutil.which("node"):
        pytest.skip("node is required to exercise the embedded UI")
    result = subprocess.run(["node", "-e", source], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


HELPERS = """
const esc = s => String(s || '').replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));
const icon = id => `<svg>${id}</svg>`;
const fileURL = name => '/f/' + encodeURIComponent(name);
const fileHTML = esc;
const msgHTML = esc;
"""


def test_review_banner_is_amber_with_escaped_reason_and_all_actions(page):
    result = run_js(HELPERS + "const S = {others:null};" + section(page, "const SCAN_STATES =", "const RESTART_SCAN_MSG") + """
const cfg = SCAN_STATES.review({file:'scan-2p.pdf',msg:'尺寸异常 <script>'});
console.log(JSON.stringify(cfg));
""")
    assert result["tone"] == "warn"
    assert result["title"] == "Review required: the scan may be incomplete"
    assert "尺寸异常 &lt;script&gt;" in result["detail"]
    assert any(a.get("download") == "scan-2p.pdf" for a in result["actions"])
    assert any(a.get("act") == "print-file" and a["label"] == "Review, then print" for a in result["actions"])


def test_review_offers_a_full_width_rescan_only_when_the_sheet_did_not_fit(page):
    result = run_js(HELPERS + "const S = {others:null};" + section(page, "const SCAN_STATES =", "const RESTART_SCAN_MSG") + """
const acts = m => SCAN_STATES.review({file:'scan-7p.pdf', msg:m}).actions.map(a => a.act || 'download');
console.log(JSON.stringify([acts('第 1-7 页：The sheet is wider than the selected size, often because it was fed sideways; both side edges are incomplete'),
                            acts('Page 1：The sheet is longer than the scan area; its trailing edge may be missing'), acts('Pages 2: 纸的right edge有内容')]));
""")
    assert result == [["download", "print-file", "scan-full"], ["download", "print-file", "scan-full"],
                      ["download", "print-file"]]
    handler = section(page, "if (act === 'scan-full') {", "if (act === 'scan-flatbed') {")
    assert "scanForm.elements.size.value = 'full'" in handler and "return startScan();" in handler
    start = section(page, "async function startScan() {", "/* ---------- scanned files")
    assert "if (!reloadForUpdate())" in start         # a refused stale page reloads only when nothing is in flight


def test_review_action_bar_keeps_download_and_rescan_without_success_label(page):
    result = run_js("""
const nodes = {};
const $ = s => nodes[s] || (nodes[s] = {classList:{toggle(){}},setAttribute(k,v){this[k]=v;}});
const S = {view:'review',data:{file:'scan-2p.pdf'},others:null,starting:false};
const scanSummary = () => ({main:'A4',sub:'settings'});
const scanForm = {elements:{source:{value:'ADF'},size:{value:'full'},res:{value:'300'}}};
const scanConflict = () => false;
const RES_HINT = {}, netDown = false;
const sizeHint = () => '';
const fileURL = name => '/f/' + name;
const bars = [], buttons = [];
const setBar = (name, data) => bars.push(data);
const setButton = (button, data) => buttons.push(data);
""" + section(page, "function updateScan() {", "/* The default size follows the source") + """
updateScan(); console.log(JSON.stringify({dl:nodes['#scan-dl'],bar:bars.at(-1),button:buttons.at(-1)}));
""")
    assert result["dl"]["hidden"] is False
    assert result["dl"]["href"] == "/f/scan-2p.pdf"
    assert result["bar"]["tone"] == "warn"
    assert "Review" in result["bar"]["main"]
    assert "Complete" not in result["bar"]["main"]
    assert result["button"]["disabled"] is False


def test_review_poll_is_terminal_and_persists_review_for_reload(page):
    result = run_js("""
const S = {job:{id:'job1',running:true},view:'busy'}, PJ = {job:null};
const P = {}; let netDown=false, netFails=0, freshName=null;
const saved = {}; const store = {set:(k,v) => saved[k]=v};
const setDeviceChip=()=>{},refreshFiles=()=>{},renderScan=()=>{},updatePrint=()=>{};
""" + section(page, "function checkBuild(build) {", "document.addEventListener('visibilitychange'") + """
applyStatus({scanner:{busy:false},scan:{state:'review',msg:'需检查尺寸',file:'scan-2p.pdf'}},'job1','');
console.log(JSON.stringify({S,saved}));
""")
    assert result["S"]["view"] == "review"
    assert result["S"]["job"] is None
    assert result["saved"]["scan-review"] == {"msg": "需检查尺寸", "file": "scan-2p.pdf"}


def test_review_file_row_is_warning_and_preserves_print_and_download(page):
    result = run_js(HELPERS + """
const nodes = {}; const $ = s => nodes[s] || (nodes[s]={});
let freshName='scan-2p.pdf',armed=null;
const files=[{name:freshName,kb:12,left_min:9,quality:{state:'review',issues:['方向 <unknown>']}}];
""" + section(page, "const fmtSize =", "let filesSeq =") + """
renderFiles(); console.log(JSON.stringify(nodes['#files'].innerHTML));
""")
    assert "is-review" in result
    assert "Review required" in result
    assert "方向 &lt;unknown&gt;" in result
    assert "Review, then print" in result
    assert 'data-act="print-file"' in result
    assert 'href="/f/scan-2p.pdf"' in result
    assert "New scan" not in result


def test_size_hints_describe_plain_scanner_output(page):
    # ADF_ROOM comes from BOOT.adf_side_room (adf_window: 5 mm beside A4, none beside A3)
    result = run_js("const labelOf=(key,v)=>v; const ADF_ROOM={A4:5, Letter:5, A3:0};"
                    + section(page, "function sizeHint(src, size) {", "/* S.view:") + """
console.log(JSON.stringify({adfFull: sizeHint('ADF','full'), adfA4: sizeHint('ADF','A4'), adfA3: sizeHint('ADF','A3'),
                            glassA5: sizeHint('Flatbed','A5'), glassFull: sizeHint('Flatbed','full')}));
""")
    assert "297 x 432" in result["adfFull"] and "larger than the sheet" in result["adfFull"]
    assert "unseen strips along both long edges" in result["adfFull"]          # A3 / sideways A4 are as wide as the feed path
    assert "5 mm extra on each side" in result["adfA4"] and "including edge seals" in result["adfA4"]
    assert "2 mm at the leading edge remains unseen" in result["adfA4"]                     # the leading end, always
    assert "unseen on each long edge" in result["adfA3"] and "preserved completely" not in result["adfA3"]
    assert "top-left" in result["glassA5"]
    assert "without cropping" in result["glassFull"]
    for hint in result.values():                       # nothing promises automatic cropping or detection any more
        assert "automatically detect paper edges" not in hint and "automatically recognize" not in hint and "every sheet is complete" not in hint


def test_scan_form_has_no_automatic_processing(page):
    """Owner, 2026-10-19: everything off - scans are the scanner's own output; the section was removed."""
    for name in ("enh", "deskew", "blank", "ocr"):
        assert 'name="%s"' % name not in page
    assert ">Automatic processing<" not in page
    summary = section(page, "function scanSummary() {", "/* only if the feeder")
    assert "Original scanner output" in summary


def test_paper_white_is_one_tick_box_on_by_default(page):
    """Owner, 2026-10-19 18:40 SGT: paper colour removed by default, coloured paper kept, original always kept."""
    assert '<input type="checkbox" name="paper_white" value="1" checked>' in page
    assert "Whiten paper" in page and "Original scans remain available" in page
    summary = section(page, "function scanSummary() {", "/* only if the feeder")
    assert "f.paper_white && f.paper_white.checked ? 'Paper whitening / original saved separately' : 'Original scanner output'" in summary
    keep = section(page, "function saveScanForm() {", "scanForm.addEventListener('change', saveScanForm);")
    assert "pw: !!(f.paper_white && f.paper_white.checked)" in keep
    assert "scanForm.elements.paper_white.checked = v.pw" in keep


def test_default_size_follows_the_source_until_the_user_picks_one(page):
    block = section(page, "let sizeTouched = false;", "scanForm.addEventListener('change', updateScan);")
    result = run_js(block.split("$('#scan-size')")[0] + """
console.log(JSON.stringify([sizeDefaultFor('ADF'), sizeDefaultFor('ADF Duplex'), sizeDefaultFor('Flatbed')]));
""")
    assert result == ["A4", "A4", "full"]
    assert "if (e.target && e.target.name === 'source' && !sizeTouched)" in block


CHECK_BUILD_STUBS = """
let reloads = 0, now = 1e9; const location = {reload(){ reloads++; }};
Date.now = () => now;
const BOOT = {build: 'new'}, RETAIN = 15, saved = {};
const store = {set: (k, v) => { saved[k] = v; }};
const saveScanForm = () => { saved['scan-form'] = 'saved'; };
const S = {starting:false, view:'idle', legacy:false, data:{}};
const P = {busy:false, converting:false, doc:null, lastUse:0, state:'none', data:{}};
const PJ = {job:null, show:false};
"""


def test_stale_page_reloads_only_when_nothing_is_in_flight(page):
    fn = section(page, "function checkBuild(build) {", "function applyStatus(d, sj, pj) {")
    result = run_js(CHECK_BUILD_STUBS + fn + """
const out = [];
out.push(checkBuild('new'), reloads);                       // same build: nothing
S.view = 'busy'; out.push(checkBuild('old'), reloads);      // scanning: wait
S.view = 'idle'; P.doc = {pages:2}; P.lastUse = now - 60e3;
out.push(checkBuild('old'), reloads);                       // a document being set up for printing: wait
P.lastUse = now - 16 * 60e3;
out.push(checkBuild('old'), reloads);                       // untouched past RETAIN (the station deleted it): reload
console.log(JSON.stringify(out));
""")
    assert result == [False, 0, False, 0, False, 0, True, 1]


def test_update_reload_keeps_what_is_on_screen(page):
    """A finished scan's banner (e.g. the A3 note that exists only there) and a print result survive the reload."""
    fn = section(page, "function checkBuild(build) {", "function applyStatus(d, sj, pj) {")
    result = run_js(CHECK_BUILD_STUBS + fn + """
S.view = 'done'; S.data = {msg: '完成：…unseen strips along both long edges', file: 'scan-1p.pdf'};
PJ.show = true; P.state = 'received'; P.data = {msg: 'Printer received the job'};
checkBuild('old');
console.log(JSON.stringify(saved));
""")
    assert result["reload"] == {"scan": {"view": "done", "data": {"msg": "完成：…unseen strips along both long edges", "file": "scan-1p.pdf"}},
                                "print": {"state": "received", "data": {"msg": "Printer received the job"}}}
    assert result["scan-form"] == "saved"
    apply = section(page, "function applyStatus(d, sj, pj) {", "document.addEventListener('visibilitychange'")
    assert "if (checkBuild(d.build)) return;" in apply      # nothing may change the saved state after the reload call
    startup = section(page, "/* ================= start ================= */", "/* UI hooks")
    assert "store.get('reload')" in startup and "store.set('reload', null)" in startup
    assert "restoreScanForm(store.get('scan-form'))" in startup


def test_scan_choices_come_back_after_a_reload(page):
    """A restored 'review' must rescan from the feeder it came from, not from the default glass."""
    block = section(page, "function restoreScanForm(v) {", "scanForm.addEventListener('change', saveScanForm);")
    result = run_js("""
const OPTS = {sources:[['Flatbed'],['ADF'],['ADF Duplex']], sizes:[['full'],['A4'],['A3']], res:[['150'],['300']],
              modes:[['Color'],['Gray']]};
const opts = k => OPTS[k];
const form = {source:'Flatbed', size:'full', res:'300', mode:'Color'};
const scanForm = {elements:{size:{set value(v){ form.size = v; }}}};
const setRadio = (f, name, v) => { form[name] = v; };
let sizeTouched = false;
""" + block + """
restoreScanForm({source:'ADF', size:'A4', res:'150', mode:'Gray', touched:false});
const first = Object.assign({}, form, {touched: sizeTouched});
restoreScanForm({source:'Bogus', size:'A0', res:'600', mode:'Sepia', touched:true});   // unknown values are ignored
console.log(JSON.stringify([first, Object.assign({}, form, {touched: sizeTouched})]));
""")
    assert result[0] == {"source": "ADF", "size": "A4", "res": "150", "mode": "Gray", "touched": False}
    assert result[1] == {"source": "ADF", "size": "A4", "res": "150", "mode": "Gray", "touched": True}
    start = section(page, "async function startScan() {", "/* ---------- scanned files")
    assert "body.append('build', BOOT.build || '')" in start and "saveScanForm();" in start
    assert "r.data.stale" in start                          # refused as another build: reload instead of an error


def test_file_row_offers_the_e_mail_copy_and_kept_originals(page):
    result = run_js(HELPERS + """
const nodes = {}; const $ = s => nodes[s] || (nodes[s]={});
let freshName=null,armed=null;
const files=[{name:'scan-1p.pdf',kb:3000,left_min:9,quality:{state:'done',issues:[]},small:true,raw:true},
             {name:'scan-2p.pdf',kb:12,left_min:9,quality:{state:'done',issues:[]},small:false,raw:false}];
""" + section(page, "const fmtSize =", "let filesSeq =") + """
renderFiles(); console.log(JSON.stringify(nodes['#files'].innerHTML));
""")
    first, second = result.split('data-name="scan-2p.pdf"')
    assert 'href="/small/scan-1p.pdf"' in first and "Smaller email copy" in first
    assert 'href="/raw/scan-1p.pdf"' in first and "Original scan" in first
    assert "/small/" not in second and "/raw/" not in second


def test_copy_preserves_quality_for_print_preview(page):
    result = run_js("""
let docSeq=0, loaded; const setTab=()=>{},clearPrintBannerForNewDoc=()=>{},setConverting=()=>{};
const window={scrollTo(){}},pagesFromName=()=>2;
const loadDocument=(d)=>loaded=d;
const api=async()=>({ok:true,data:{token:'token',pages:2,quality:{state:'review',issues:['方向需检查']}}});
""" + section(page, "async function copyToPrint(name) {", "$('#file-input').addEventListener") + """
copyToPrint('scan-2p.pdf').then(()=>console.log(JSON.stringify(loaded)));
""")
    assert result["quality"] == {"state": "review", "issues": ["方向需检查"]}


def test_saved_review_restores_only_while_pdf_exists_and_new_scan_clears_it(page):
    startup = section(page, "/* ================= start ================= */", "/* UI hooks")
    assert "store.get('scan-review')" in startup
    assert "files.some" in startup
    assert "S.view = 'review'" in startup
    start = section(page, "async function startScan() {", "/* ---------- scanned files")
    assert "store.set('scan-review', null)" in start
    assert 'id="doc-quality"' in page
    assert "setQualityNote(d.quality)" in page
    converting = section(page, "function setConverting(", "/* paper sizes in mm")
    assert "setQualityNote(null)" in converting


def test_embedded_javascript_syntax(page, tmp_path):
    if not shutil.which("node"):
        pytest.skip("node is required for JavaScript syntax verification")
    script = page.split("<script>", 1)[1].split("</script>", 1)[0]
    path = tmp_path / "station.js"
    path.write_text(script.replace("__BOOT_JSON__", "{}"))
    result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
