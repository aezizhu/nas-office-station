"""Exercise document-loading paper choices without sending a physical print job."""

import json
import shutil
import subprocess

import pytest


def load_document(m, size, kind="upload", action=""):
    if not shutil.which("node"):
        pytest.skip("node is required to exercise the embedded UI")
    page = m.PAGE
    code = page[page.index("const SIZE_MM ="):page.index("function setQualityNote(")]
    script = """
const nodes = {};
const $ = selector => nodes[selector] || (nodes[selector] = {
  hidden: false, textContent: '', value: '', children: {},
  classList: {toggle() {}, remove() {}},
  querySelector(name) {
    return this.children[name] || (this.children[name] = {textContent: '', setAttribute() {}});
  },
});
const P = {}, DEF = {paper: 'A4', sides: 'None'};
const printForm = {elements: {
  paper: {value: 'A4', options: [{value: 'A4'}, {value: 'A3'}, {value: 'A5'}]},
  scale: {value: 'auto'}, sides: {value: 'None'},
}};
const pagesInput = {}, copiesInput = {}, thumbsEl = $('#thumbs');
const closeLightbox = () => {}, setQualityNote = () => {}, onCopies = () => {};
const updateScaleHint = () => {}, renderThumbs = () => {}, syncThumbs = () => {}, updatePrint = () => {};
const setRadio = (form, name, value) => { form.elements[name].value = value; };
const scaleV = () => printForm.elements.scale.value;
const showDocbar = (name, meta) => { $('#doc-meta').textContent = meta; };
""" + code + "\nloadDocument(" + json.dumps({
        "name": "example.pdf", "pages": 1, "size": size, "token": "a" * 32,
    }) + ", " + json.dumps({"kind": kind}) + ");\n" + action + """
console.log(JSON.stringify({
  paper: printForm.elements.paper.value,
  scale: scaleV(),
  sourceSize: P.doc.size,
  hint: $('#paper-hint').querySelector('span').textContent,
  hintHidden: $('#paper-hint').hidden,
  note: $('#paper-note').querySelector('span').textContent,
  noteHidden: $('#paper-note').hidden,
}));
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a3_upload_keeps_a4_paper_and_explains_scaling(m):
    result = load_document(m, "A3")
    assert result["paper"] == "A4"
    assert result["scale"] == "auto" and result["sourceSize"] == "A3"
    assert not result["hintHidden"]
    assert "A3" in result["hint"] and "reduced proportionally to fit A4" in result["hint"]
    assert not result["noteHidden"]
    assert "manually" in result["note"] and "A3" in result["note"] and "tray" in result["note"]


def test_a3_upload_note_survives_explicit_paper_selection(m):
    result = load_document(m, "A3", action="printForm.elements.paper.value = 'A3'; setPaperHint(null);")
    assert result["paper"] == "A3" and result["hintHidden"]
    assert not result["noteHidden"]
    assert "tray" in result["note"] and "A3" in result["note"]


def test_a3_copy_keeps_a4_fit_and_existing_glass_note(m):
    result = load_document(m, "A3", kind="copy")
    assert result["paper"] == "A4" and result["scale"] == "fit"
    assert not result["noteHidden"] and "glass" in result["note"]


@pytest.mark.parametrize("size, hint_fragment", [("A4", None), ("A5", "actual size"), ("Letter", "reduced proportionally to fit A4")])
def test_ordinary_upload_defaults_are_unchanged(m, size, hint_fragment):
    result = load_document(m, size)
    assert result["paper"] == "A4" and result["scale"] == "auto"
    assert result["noteHidden"]
    if hint_fragment is None:
        assert result["hintHidden"]
    else:
        assert not result["hintHidden"] and hint_fragment in result["hint"]
