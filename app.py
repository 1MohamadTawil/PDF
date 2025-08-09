#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF-Designer + Filler (mit visueller Feld-Vorschau)
- / -> Filler (liest/füllt AcroForm falls vorhanden)
- /designer -> PDF als Bild + Live-Overlay-Rechtecke bei Klick
- /build -> erzeugt NEUE fillable PDF anhand der gesetzten Felder
"""
import io
import os
import json
import tempfile
from typing import Dict

from flask import Flask, request, redirect, url_for, render_template_string, send_file, session, flash, jsonify
from werkzeug.utils import secure_filename

from pdfrw import PdfReader, PdfWriter, PdfDict, PdfName, PdfObject, IndirectPdfDict
import fitz  # PyMuPDF
from jinja2 import DictLoader

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", tempfile.gettempdir())
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB

# ---------------- UI Templates ----------------
TPL_LAYOUT = """
<!doctype html>
<html lang=\"de\">
  <head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>PDF Formular</title>
    <style>
      :root { --accent: #ff3b30; }
      body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 20px; }
      .container { max-width: 1100px; margin: 0 auto; }
      .card { border: 1px solid #ddd; border-radius: 14px; padding: 16px; margin-bottom: 16px; }
      .row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
      .muted { color: #666; font-size: 0.9rem; }
      button, .btn { background: black; color: white; border: none; padding: 10px 14px; border-radius: 10px; cursor: pointer; text-decoration: none; }
      input[type=text], input[type=number], select { padding: 8px; border: 1px solid #ccc; border-radius: 8px; }
      .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
      .flash { background: #fff7cc; border: 1px solid #ffe680; padding: 8px 12px; border-radius: 8px; margin-bottom: 12px; }
      .canvas-wrap { position: relative; display: inline-block; }
      img.page { display:block; max-width: 100%; border: 1px solid #ddd; border-radius: 12px; }
      .overlay { position:absolute; top:0; left:0; pointer-events:none; }
      .rect { position:absolute; border: 2px dashed var(--accent); background: rgba(255,59,48,.06); pointer-events:none; }
      .pill { position:absolute; transform: translate(-50%, -100%); background: var(--accent); color:#fff; font-size:11px; padding:2px 6px; border-radius: 999px; white-space:nowrap; pointer-events:none;}
      .toolbar { display: flex; gap: 10px; margin-bottom: 10px; align-items: center; flex-wrap:wrap; }
      .small { font-size: 12px; }
      .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    </style>
  </head>
  <body>
    <div class=\"container\">
      {% with messages = get_flashed_messages() %}
        {% if messages %}
          {% for msg in messages %}<div class=\"flash\">{{ msg }}</div>{% endfor %}
        {% endif %}
      {% endwith %}
      {% block content %}{% endblock %}
    </div>
  </body>
</html>
"""

TPL_INDEX = """
{% extends \"layout.html\" %}
{% block content %}
  <div class=\"card\">
    <h2>PDF hochladen</h2>
    <form action=\"{{ url_for('upload') }}\" method=\"post\" enctype=\"multipart/form-data\" class=\"row\">
      <input type=\"file\" name=\"pdf\" accept=\"application/pdf\" required>
      <button type=\"submit\">Hochladen</button>
      {% if pdf_name %}<span class=\"muted\">Aktuell: {{ pdf_name }}</span>{% endif %}
    </form>
    <div class=\"row\" style=\"margin-top:10px\">
      <a class=\"btn\" href=\"{{ url_for('designer') }}\">Designer öffnen</a>
      <a class=\"btn\" href=\"{{ url_for('index') }}\">Neu starten</a>
    </div>
  </div>

  {% if fields %}
  <div class=\"card\">
    <h2>Erkannte Felder (AcroForm)</h2>
    <form action=\"{{ url_for('fill') }}\" method=\"post\">
      <div class=\"grid\">
        {% for f in fields %}
          <div>
            <label>{{ f.label }}</label>
            <input type=\"text\" name=\"{{ f.name }}\" value=\"{{ f.value or '' }}\" placeholder=\"{{ f.name }}\">
          </div>
        {% endfor %}
      </div>
      <div class=\"row\" style=\"margin-top:12px\">
        <button type=\"submit\">PDF erzeugen</button>
      </div>
    </form>
  </div>
  {% else %}
    <p class=\"muted\">Dieses PDF hat keine AcroForm-Felder. Nutze den Designer, um Felder zu definieren und eine neue „fillable“ PDF zu erzeugen.</p>
  {% endif %}
{% endblock %}
"""

TPL_DESIGNER = """
{% extends \"layout.html\" %}
{% block content %}
  <div class=\"card\">
    <h2>Designer: Felder definieren</h2>
    <div class=\"toolbar\">
      <label>Feldname: <input type=\"text\" id=\"fname\" placeholder=\"z.B. kunde_name\"></label>
      <label>Breite: <input type=\"number\" id=\"fwidth\" value=\"180\" min=\"10\" class=\"small\"></label>
      <label>Höhe: <input type=\"number\" id=\"fheight\" value=\"20\" min=\"8\" class=\"small\"></label>
      <button onclick=\"saveTemplate()\">Template speichern</button>
      <form action=\"{{ url_for('build') }}\" method=\"post\" style=\"display:inline\">
        <button type=\"submit\">Neue fillable PDF erzeugen</button>
      </form>
      <a class=\"btn\" href=\"{{ url_for('index') }}\">Zurück</a>
      <span class=\"muted small\">Tipp: Zoomen mit Browser (cmd/strg + / -)</span>
    </div>
    <p class=\"muted\">Klicke auf die Seite, um ein Feld zu setzen. Jedes Feld erscheint sofort als rotes Rechteck.</p>
  </div>

  {% for i in range(1, page_count+1) %}
    <div class=\"card\">
      <h3>Seite {{ i }}</h3>
      <div class=\"canvas-wrap\" id=\"wrap{{ i }}\">
        <img class=\"page\" id=\"img{{ i }}\" src=\"{{ url_for('page_png', pageno=i) }}\" onclick=\"placeField({{ i }}, event)\" />
        <div class=\"overlay\" id=\"ov{{ i }}\"></div>
      </div>
    </div>
  {% endfor %}

  <div class=\"card\">
    <h3>Aktuelles Template ({{ template_name }})</h3>
    <pre class=\"mono small\" id=\"templatePre\">{{ template_json }}</pre>
    <div class=\"row\">
      <button onclick=\"undo()\">Letztes Feld entfernen</button>
      <button onclick=\"clearAll()\">Alle Felder löschen</button>
    </div>
  </div>

  <script>
    const template = {{ template_json | safe }};

    function drawOverlay() {
      // Leeren
      document.querySelectorAll('.overlay').forEach(ov => ov.innerHTML = '');
      if (!template.fields) return;
      for (const f of template.fields) {
        const wrap = document.getElementById('wrap' + f.page);
        const img = document.getElementById('img' + f.page);
        const ov = document.getElementById('ov' + f.page);
        if (!wrap || !img || !ov) continue;
        // Skaliere von natural -> aktuell
        const sx = img.width / img.naturalWidth;
        const sy = img.height / img.naturalHeight;
        const left = f.x * sx;
        const top = f.y * sy;
        const w = f.w * sx;
        const h = f.h * sy;
        const rect = document.createElement('div');
        rect.className = 'rect';
        rect.style.left = left + 'px';
        rect.style.top = top + 'px';
        rect.style.width = w + 'px';
        rect.style.height = h + 'px';
        const pill = document.createElement('div');
        pill.className = 'pill';
        pill.style.left = (left + w/2) + 'px';
        pill.style.top = top + 'px';
        pill.textContent = f.name;
        ov.appendChild(rect);
        ov.appendChild(pill);
        // Größe der Overlay-Fläche an Bild anpassen
        ov.style.width = img.clientWidth + 'px';
        ov.style.height = img.clientHeight + 'px';
      }
    }

    function placeField(pageNo, ev) {
      const img = document.getElementById('img' + pageNo);
      const rect = img.getBoundingClientRect();
      const scaleX = img.naturalWidth / img.width;
      const scaleY = img.naturalHeight / img.height;
      const x = (ev.clientX - rect.left) * scaleX;
      const y = (ev.clientY - rect.top) * scaleY;

      const name = document.getElementById('fname').value.trim();
      const w = parseFloat(document.getElementById('fwidth').value) || 180;
      const h = parseFloat(document.getElementById('fheight').value) || 20;
      if (!name) { alert('Bitte Feldname eingeben'); return; }

      if (!template.fields) template.fields = [];
      template.fields.push({page: pageNo, x: x, y: y, w: w, h: h, name: name, type: "text"});
      document.getElementById('templatePre').textContent = JSON.stringify(template, null, 2);
      drawOverlay();
    }

    function undo() {
      if (template.fields && template.fields.length) {
        template.fields.pop();
        document.getElementById('templatePre').textContent = JSON.stringify(template, null, 2);
        drawOverlay();
      }
    }

    function clearAll() {
      template.fields = [];
      document.getElementById('templatePre').textContent = JSON.stringify(template, null, 2);
      drawOverlay();
    }

    async function saveTemplate() {
      const res = await fetch('{{ url_for("save_template") }}', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(template)
      });
      if (res.ok) alert('Template gespeichert');
      else alert('Fehler beim Speichern');
    }

    // Neu zeichnen nach Bild-Load / Resize
    window.addEventListener('load', drawOverlay);
    window.addEventListener('resize', drawOverlay);
  </script>
{% endblock %}
"""

# ---------------- Template Loader ----------------
app.jinja_loader = DictLoader({
    "layout.html": TPL_LAYOUT,
    "index.html": TPL_INDEX,
    "designer.html": TPL_DESIGNER
})

# ---------------- Helper ----------------
def _set_need_appearances(pdf):
    if not getattr(pdf.Root, 'AcroForm', None):
        pdf.Root.AcroForm = PdfDict()
    pdf.Root.AcroForm.update(PdfDict(NeedAppearances=PdfObject('true')))

def _get_widgets(pdf):
    fields = {}
    if not getattr(pdf.Root, 'AcroForm', None):
        return fields, {}
    for page in pdf.pages:
        annots = getattr(page, 'Annots', []) or []
        for annot in annots:
            if annot.get('/Subtype') != PdfName('Widget'):
                continue
            name = (annot.get('/T') or '').strip('()') if annot.get('/T') else None
            if not name:
                continue
            fields[name] = annot
    return fields, {}

def _field_desc(name, widget, _radio_selected):
    ft = widget.get('/FT')
    label = widget.get('/TU') or name
    if ft == PdfName('Tx'):
        value = widget.get('/V')
        if value is not None:
            value = str(value).strip('()')
        return dict(type='text', name=name, label=label, value=value)
    return dict(type='text', name=name, label=label, value=None)

def _ensure_upload():
    pdf_path = session.get('pdf_path')
    if not pdf_path or not os.path.exists(pdf_path):
        return None
    return pdf_path

# ---------------- Routes: Filler ----------------
@app.route('/')
def index():
    fields = session.get('fields_render', [])
    pdf_name = os.path.basename(session.get('pdf_path')) if session.get('pdf_path') else None
    return render_template_string(TPL_INDEX, fields=fields, pdf_name=pdf_name)

@app.route('/upload', methods=['POST'])
def upload():
    f = request.files.get('pdf')
    if not f or f.filename == '':
        flash('Keine Datei ausgewählt.')
        return redirect(url_for('index'))
    filename = secure_filename(f.filename)
    temp_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    f.save(temp_path)
    session['pdf_path'] = temp_path

    try:
        pdf = PdfReader(temp_path)
    except Exception as e:
        flash(f'PDF konnte nicht gelesen werden: {e}')
        return redirect(url_for('index'))

    _set_need_appearances(pdf)
    widgets, radio_selected = _get_widgets(pdf)
    fields_render = [_field_desc(name, w, radio_selected) for name, w in widgets.items()]
    session['fields_render'] = fields_render
    flash(f"{len(fields_render)} AcroForm-Feld(er) erkannt.{'' if len(fields_render)>0 else ' (keine)'}")
    return redirect(url_for('index'))

@app.route('/fill', methods=['POST'])
def fill():
    pdf_path = _ensure_upload()
    if not pdf_path:
        flash('Sitzung abgelaufen. Bitte PDF erneut hochladen.')
        return redirect(url_for('index'))

    values = dict(request.form.items())
    pdf = PdfReader(pdf_path)
    _set_need_appearances(pdf)
    fields, _ = _get_widgets(pdf)
    for name, widget in fields.items():
        if name in values:
            widget.update(PdfDict(V=str(values[name])))

    out_io = io.BytesIO()
    PdfWriter().write(out_io, pdf)
    out_io.seek(0)
    out_name = os.path.splitext(os.path.basename(pdf_path))[0] + "_ausgefuellt.pdf"
    return send_file(out_io, as_attachment=True, download_name=out_name, mimetype='application/pdf')

# ---------------- Routes: Designer ----------------
@app.route('/designer')
def designer():
    pdf_path = _ensure_upload()
    if not pdf_path:
        flash('Bitte zuerst eine PDF hochladen.')
        return redirect(url_for('index'))
    doc = fitz.open(pdf_path)
    tmpl = session.get('template') or {"fields": [], "page_sizes": []}
    if not tmpl["page_sizes"]:
        for p in doc:
            rect = p.rect
            tmpl["page_sizes"].append([rect.width, rect.height])
    session['template'] = tmpl
    return render_template_string(TPL_DESIGNER,
                                  page_count=len(doc),
                                  template_json=json.dumps(tmpl),
                                  template_name="session_template.json")

@app.route('/page/<int:pageno>')
def page_png(pageno: int):
    pdf_path = _ensure_upload()
    if not pdf_path:
        return "no file", 400
    doc = fitz.open(pdf_path)
    if pageno < 1 or pageno > len(doc):
        return "bad page", 404
    page = doc[pageno-1]
    zoom = 2.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img_bytes = pix.tobytes("png")
    return send_file(io.BytesIO(img_bytes), mimetype="image/png")

@app.route('/save-template', methods=['POST'])
def save_template():
    data = request.get_json(force=True)
    session['template'] = data
    return jsonify({"ok": True})

@app.route('/build', methods=['POST'])
def build():
    pdf_path = _ensure_upload()
    if not pdf_path:
        flash('Bitte zuerst eine PDF hochladen.')
        return redirect(url_for('index'))
    tmpl = session.get('template') or {"fields": [], "page_sizes": []}
    if not tmpl["fields"]:
        flash("Kein Feld im Template. Im Designer per Klick Felder hinzufügen.")
        return redirect(url_for('designer'))

    pdf = PdfReader(pdf_path)
    _set_need_appearances(pdf)

    for idx, page in enumerate(pdf.pages, start=1):
        page_annots = getattr(page, 'Annots', None)
        if page_annots is None:
            page.Annots = page_annots = []

        page_fields = [f for f in tmpl["fields"] if int(f["page"]) == idx]
        page_w, page_h = tmpl["page_sizes"][idx-1]
        for fld in page_fields:
            x = float(fld["x"]); y = float(fld["y"]); w = float(fld["w"]); h = float(fld["h"])
            pdf_y = page_h - y - h
            rect = [x, pdf_y, x+w, pdf_y+h]

            tf = IndirectPdfDict(
                FT=PdfName('Tx'),
                T='({})'.format(fld["name"]),
                V='',
                Ff=0,
                DA='(/Helv 10 Tf 0 g)',
                Rect=rect,
                Subtype=PdfName('Widget'),
                Type=PdfName('Annot'),
                F=4
            )
            page_annots.append(tf)

    out_io = io.BytesIO()
    PdfWriter().write(out_io, pdf)
    out_io.seek(0)
    out_name = os.path.splitext(os.path.basename(pdf_path))[0] + "_fillable.pdf"
    return send_file(out_io, as_attachment=True, download_name=out_name, mimetype='application/pdf')

@app.route('/health')
def health():
    return "ok", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
