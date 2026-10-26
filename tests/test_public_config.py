"""Public defaults must never identify or discover an internal device."""
import ast
from pathlib import Path


def default_value(tree, name):
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == name for target in node.targets))
    return ast.literal_eval(assignment.value.args[1])


def test_no_private_device_defaults():
    tree = ast.parse((Path(__file__).parents[1] / 'app/scan_app.py').read_text())
    assert default_value(tree, 'DEV') == ''
    assert default_value(tree, 'PRINTER_IP') == ''


def test_unconfigured_scanner_does_not_probe(m, monkeypatch):
    monkeypatch.setattr(m, 'DEV', '')
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError('Unconfigured scanner performed discovery')
    monkeypatch.setattr(m.subprocess, 'run', forbidden)
    assert m.smfp_device() is None
    assert calls == []


def test_unconfigured_scan_request_is_refused_before_hardware_work(m, client, monkeypatch):
    monkeypatch.setattr(m, 'DEV', '')
    response = client.post('/scan', data={'build': m.BUILD_ID})
    assert response.status_code == 503
    assert 'STATION_DEV' in response.json['error']
    assert not m.scan_lock.locked()


def test_default_browser_labels_and_options_are_english(m, client):
    import json
    import re
    from html.parser import HTMLParser

    class Labels(HTMLParser):
        def __init__(self):
            super().__init__()
            self.hidden = 0
            self.labels = []
        def handle_starttag(self, tag, attrs):
            if tag in ('script', 'style'):
                self.hidden += 1
            if not self.hidden:
                self.labels.extend(value for key, value in attrs
                                   if key in ('aria-label', 'placeholder', 'title') and value)
        def handle_endtag(self, tag):
            if tag in ('script', 'style'):
                self.hidden -= 1
        def handle_data(self, text):
            if not self.hidden:
                self.labels.append(text)

    page = client.get('/').get_data(as_text=True)
    labels = Labels()
    labels.feed(page)
    assert not re.search(r'[\u4e00-\u9fff]', ' '.join(labels.labels))
    assert '<html lang="en">' in page
    assert 'HP LaserJet MFP M437nda' in page
    boot = json.loads(m._boot_json())
    for key in ('sources', 'modes', 'sizes', 'sides', 'papers'):
        assert not re.search(r'[\u4e00-\u9fff]', json.dumps(boot.get(key), ensure_ascii=False))
