#!/usr/bin/env python3
from flask import Flask, request, redirect, url_for, render_template_string, send_file, session, flash
from werkzeug.utils import secure_filename
from pdfrw import PdfReader, PdfWriter, PdfDict, PdfName, PdfObject
import io
import os
import tempfile
from typing import Dict, List, Tuple

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
  </head>
  <body>
    <h1>PDF Formular Füller</h1>
    {% with messages = get_flashed_messages() %}
      {% if messages %}
        {% for msg in messages %}<div>{{ msg }}</div>{% endfor %}
      {% endif %}
    {% endwith %}
    {% block content %}{% endblock %}
  </body>
</html>
"""

TPL_INDEX = """
{% extends "layout" %}
{% block content %}
  <form action="{{ url_for('upload') }}" method="post" enctype="multipart/form-data">
    <input type="file" name="pdf" accept="application/pdf" required>
    <button type="submit">Hochladen</button>
  </form>
  {% if fields %}
    <form action="{{ url_for('fill') }}" method="post">
      {% for f in fields %}
        <label>{{ f.name }}</label>
        <input type="text" name="{{ f.name }}" value="{{ f.value or '' }}">
      {% endfor %}
      <button type="submit">PDF erzeugen</button>
    </form>
  {% endif %}
{% endblock %}
"""

def _get_acroform_fields(pdf) -> Dict[str, PdfDict]:
    fields = {}
    if not getattr(pdf.Root, 'AcroForm', None):
        return fields
    for page in pdf.pages:
        annots = getattr(page, 'Annots', []) or []
        for annot in annots:
            if annot.get('/Subtype') != PdfName('Widget'):
                continue
            name = (annot.get('/T') or '').strip('()') if annot.get('/T') else None
            if not name:
                continue
            fields[name] = annot
    return fields

def _set_need_appearances(pdf):
    if not getattr(pdf.Root, 'AcroForm', None):
        pdf.Root.AcroForm = PdfDict()
    pdf.Root.AcroForm.update(PdfDict(NeedAppearances=PdfObject('true')))

def _apply_values(pdf, values: Dict[str, str]):
    fields = _get_acroform_fields(pdf)
    for name, widget in fields.items():
        if name in values:
            widget.update(PdfDict(V=str(values[name])))

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
    widgets = _get_acroform_fields(pdf)
    fields_render = [{'name': name, 'value': None} for name in widgets.keys()]
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
    pdf = PdfReader(pdf_path)
    _set_need_appearances(pdf)
    _apply_values(pdf, values)
    out_io = io.BytesIO()
    PdfWriter().write(out_io, pdf)
    out_io.seek(0)
    out_name = os.path.splitext(os.path.basename(pdf_path))[0] + "_ausgefuellt.pdf"
    return send_file(out_io, as_attachment=True, download_name=out_name, mimetype='application/pdf')

@app.before_request
def ensure_templates_loaded():
    app.jinja_env.globals['layout'] = app.jinja_env.from_string(TPL_LAYOUT)

@app.route('/health')
def health():
    return "ok", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
