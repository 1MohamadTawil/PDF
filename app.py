#!/usr/bin/env python3
from flask import Flask, request, redirect, url_for, render_template_string, send_file, session, flash
from werkzeug.utils import secure_filename
from pdfrw import PdfReader, PdfWriter, PdfDict, PdfName, PdfObject
import io
import os
import tempfile
from typing import Dict, List, Tuple
from jinja2 import DictLoader

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", tempfile.gettempdir())
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 30 * 1024 * 1024  # 30 MB

TPL_LAYOUT = """
<!doctype html>
<html lang="de">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>PDF Formular Füller</title>
    <style>
      body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; }
      .container { max-width: 960px; margin: 0 auto; }
      header { margin-bottom: 1.5rem; }
      .card { border: 1px solid #ddd; border-radius: 14px; padding: 16px; margin-bottom: 16px; }
      .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
      label { font-weight: 600; font-size: 0.95rem; }
      input[type=text], select { width: 100%; padding: 8px; border: 1px solid #ccc; border-radius: 8px; }
      .row { display: flex; align-items: center; gap: 8px; }
      .actions { display: flex; gap: 12px; }
      button, .btn { background: black; color: white; border: none; padding: 10px 14px; border-radius: 10px; cursor: pointer; }
      .muted { color: #666; font-size: 0.9rem; }
      .checkbox { display: flex; align-items: center; gap: 8px; }
      .flash { background: #fff7cc; border: 1px solid #ffe680; padding: 8px 12px; border-radius: 8px; margin-bottom: 12px; }
      @media (max-width: 720px) { .grid { grid-template-columns: 1fr; } }
    </style>
  </head>
  <body>
    <div class="container">
      <header>
        <h1>PDF Formular Füller</h1>
        <p class="muted">PDF hochladen → Felder erscheinen als Web‑Form → Ausfüllen → Ausgefüllte PDF herunterladen.</p>
      </header>
      {% with messages = get_flashed_messages() %}
        {% if messages %}
          {% for msg in messages %}<div class="flash">{{ msg }}</div>{% endfor %}
        {% endif %}
      {% endwith %}
      {% block content %}{% endblock %}
      <footer class="muted" style="margin-top:2rem">
        <p>Hinweis: Manche PDFs brauchen zusätzliches "Flatten" (z. B. via Ghostscript). Diese App setzt <code>NeedAppearances</code>.</p>
      </footer>
    </div>
  </body>
</html>
"""

TPL_INDEX = """
{% extends "layout.html" %}
{% block content %}
  <div class="card">
    <h2>1) PDF hochladen</h2>
    <form action="{{ url_for('upload') }}" method="post" enctype="multipart/form-data">
      <div class="row" style="gap:12px; flex-wrap:wrap;">
        <input type="file" name="pdf" accept="application/pdf" required>
        <button type="submit">Hochladen</button>
      </div>
    </form>
    <p class="muted">Unterstützt: AcroForm‑Felder (Text, Checkbox, Radio, Dropdown).</p>
  </div>
  {% if fields %}
    <div class="card">
      <h2>2) Felder ausfüllen</h2>
      <form action="{{ url_for('fill') }}" method="post">
        <div class="grid">
          {% for f in fields %}
            <div>
              <label>{{ f.label or f.name }}</label>
              {% if f.type == 'text' %}
                <input type="text" name="{{ f.name }}" value="{{ f.value or '' }}" placeholder="{{ f.name }}">
              {% elif f.type == 'checkbox' %}
                <div class="checkbox">
                  <input type="checkbox" name="{{ f.name }}" value="Yes" {% if f.value in ['Yes','On','1',True] %}checked{% endif %}>
                  <span class="muted">aktivieren</span>
                </div>
              {% elif f.type == 'radio' %}
                <div class="checkbox">
                  <input type="radio" name="{{ f.group }}" value="{{ f.name }}" {% if f.selected %}checked{% endif %}>
                  <span class="muted">{{ f.name }}</span>
                </div>
              {% elif f.type == 'choice' %}
                <select name="{{ f.name }}">
                  {% for opt in f.options %}
                    <option value="{{ opt }}" {% if f.value == opt %}selected{% endif %}>{{ opt }}</option>
                  {% endfor %}
                </select>
              {% endif %}
            </div>
          {% endfor %}
        </div>
        <div class="actions" style="margin-top: 16px;">
          <button type="submit">PDF erzeugen</button>
          <a class="btn" href="{{ url_for('index') }}">Neu starten</a>
        </div>
      </form>
    </div>
  {% endif %}
{% endblock %}
"""

def _get_acroform_fields(pdf):
    fields = {}
    radio_groups = {}
    if not getattr(pdf.Root, 'AcroForm', None):
        return fields, radio_groups
    for page in pdf.pages:
        annots = getattr(page, 'Annots', []) or []
        for annot in annots:
            if annot.get('/Subtype') != PdfName('Widget'):
                continue
            name = (annot.get('/T') or '').strip('()') if annot.get('/T') else None
            field_type = annot.get('/FT')
            if not name and field_type == PdfName('Btn'):
                parent = annot.get('/Parent')
                if parent and parent.get('/T'):
                    name = parent.get('/T').strip('()')
            if not name:
                continue
            fields[name] = annot
            if field_type == PdfName('Btn'):
                parent = annot.get('/Parent')
                if parent and parent.get('/V'):
                    v = parent.get('/V')
                    if isinstance(v, PdfName):
                        radio_groups[parent.get('/T').strip('()')] = v[1:]
    return fields, radio_groups

def _field_descriptor(name, widget, radio_selected):
    ft = widget.get('/FT')
    label = widget.get('/TU') or name
    if ft == PdfName('Tx'):
        value = widget.get('/V')
        if isinstance(value, PdfObject):
            value = str(value).strip('()')
        return {'type': 'text', 'name': name, 'label': label, 'value': value}
    if ft == PdfName('Btn'):
        parent = widget.get('/Parent')
        ap = widget.get('/AP')
        n_ap = ap.get('/N') if ap else None
        export_states = []
        if isinstance(n_ap, PdfDict):
            export_states = [k[1:] for k in n_ap.keys() if isinstance(k, PdfName) and k != PdfName('Off')]
        if parent and parent.get('/Kids'):
            group = parent.get('/T').strip('()') if parent.get('/T') else name
            selected = radio_selected.get(group) == (widget.get('/AS')[1:] if isinstance(widget.get('/AS'), PdfName) else None)
            return {'type': 'radio', 'group': group, 'name': name, 'label': label, 'selected': selected}
        else:
            v = widget.get('/V')
            is_checked = False
            if isinstance(v, PdfName):
                is_checked = (v != PdfName('Off'))
            elif isinstance(widget.get('/AS'), PdfName):
                is_checked = (widget.get('/AS') != PdfName('Off'))
            return {'type': 'checkbox', 'name': name, 'label': label, 'value': 'Yes' if is_checked else 'Off'}
    if ft == PdfName('Ch'):
        opts_raw = widget.get('/Opt')
        options = []
        if isinstance(opts_raw, list):
            for o in opts_raw:
                s = str(o).strip('()')
                options.append(s)
        value = widget.get('/V')
        if isinstance(value, PdfObject):
            value = str(value).strip('()')
        return {'type': 'choice', 'name': name, 'label': label, 'options': options, 'value': value}
    return {'type': 'text', 'name': name, 'label': label, 'value': None}

def _set_need_appearances(pdf):
    if not getattr(pdf.Root, 'AcroForm', None):
        pdf.Root.AcroForm = PdfDict()
    pdf.Root.AcroForm.update(PdfDict(NeedAppearances=PdfObject('true')))

def _apply_values(pdf, values: Dict[str, str]):
    fields, _ = _get_acroform_fields(pdf)
    for name, widget in fields.items():
        if name not in values:
            continue
        v = values[name]
        ft = widget.get('/FT')
        if ft == PdfName('Tx'):
            widget.update(PdfDict(V=str(v)))
        elif ft == PdfName('Btn'):
            parent = widget.get('/Parent')
            ap = widget.get('/AP')
            n_ap = ap.get('/N') if ap else None
            on_state = None
            if isinstance(n_ap, PdfDict):
                for k in n_ap.keys():
                    if isinstance(k, PdfName) and k != PdfName('Off'):
                        on_state = k
                        break
            if not (parent and parent.get('/Kids')):
                if v in ('Yes', 'On', '1', True, 'true', 'TRUE', 'yes'):
                    if on_state is None:
                        on_state = PdfName('Yes')
                    widget.update(PdfDict(V=on_state, AS=on_state))
                else:
                    widget.update(PdfDict(V=PdfName('Off'), AS=PdfName('Off')))
            else:
                selected_widget_name = values.get(parent.get('/T').strip('()')) if parent and parent.get('/T') else None
                if selected_widget_name == name:
                    if on_state is None:
                        on_state = PdfName('Yes')
                    parent.update(PdfDict(V=on_state))
                    widget.update(PdfDict(AS=on_state))
                else:
                    widget.update(PdfDict(AS=PdfName('Off')))
        elif ft == PdfName('Ch'):
            widget.update(PdfDict(V=str(v)))

@app.route('/')
def index():
    fields = session.get('fields_render', [])
    return render_template_string(TPL_INDEX, fields=fields)

@app.route('/upload', methods=['POST'])
def upload():
    f = request.files.get('pdf')
    if not f or f.filename == '':
        flash('Keine Datei ausgewählt.')
        return redirect(url_for('index'))
    filename = secure_filename(f.filename)
    temp_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    f.save(temp_path)
    try:
        pdf = PdfReader(temp_path)
    except Exception as e:
        flash(f'PDF konnte nicht gelesen werden: {e}')
        return redirect(url_for('index'))
    _set_need_appearances(pdf)
    widgets, radio_selected = _get_acroform_fields(pdf)
    fields_render = []
    radio_added = set()
    for name, w in widgets.items():
        desc = _field_descriptor(name, w, radio_selected)
        if desc['type'] == 'radio':
            key = (desc['group'], desc['name'])
            if key in radio_added:
                continue
            radio_added.add(key)
        fields_render.append(desc)
    session['pdf_path'] = temp_path
    session['fields_render'] = fields_render
    flash(f"{len(fields_render)} Feld(er) erkannt in {filename}.")
    return redirect(url_for('index'))

@app.route('/fill', methods=['POST'])
def fill():
    pdf_path = session.get('pdf_path')
    if not pdf_path or not os.path.exists(pdf_path):
        flash('Sitzung abgelaufen. Bitte PDF erneut hochladen.')
        return redirect(url_for('index'))
    values: Dict[str, str] = {}
    for k, v in request.form.items():
        values[k] = v
    for f in session.get('fields_render', []):
        if f['type'] == 'checkbox' and f['name'] not in values:
            values[f['name']] = 'Off'
    pdf = PdfReader(pdf_path)
    _set_need_appearances(pdf)
    _apply_values(pdf, values)
    out_io = io.BytesIO()
    PdfWriter().write(out_io, pdf)
    out_io.seek(0)
    out_name = os.path.splitext(os.path.basename(pdf_path))[0] + "_ausgefuellt.pdf"
    return send_file(out_io, as_attachment=True, download_name=out_name, mimetype='application/pdf')

# Properly register inline templates via DictLoader
app.jinja_loader = DictLoader({
    "layout.html": TPL_LAYOUT
})

@app.route('/health')
def health():
    return "ok", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
