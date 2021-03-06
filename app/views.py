from flask import render_template, flash, jsonify, request, abort, Response, send_file
from app import app
from .forms import IocForm, FileForm
from config import DATA_TYPES, CORTEX, CORTEX_API, VALIDATE
from werkzeug.utils import secure_filename
from datetime import datetime
from markupsafe import Markup
import urllib
import json
import requests
import re
import pprint
import ipaddress
import csv
import io


def _validate_observable(observable):
    checked_type = None
    if not observable:
        return checked_type
    for data_type, rex in VALIDATE.iteritems():
        if rex and re.search(rex, observable):
            checked_type = data_type
    return checked_type


def _get_analyzers(data_type):
    analyzers = {}
    headers = {'Authorization': 'Bearer %s' % CORTEX_API}
    try:
        r = requests.get('%s/api/analyzer/type/%s' % (CORTEX, data_type), headers=headers, verify=False)
        analyzer_json = r.json()
        for a in analyzer_json:
            analyzers[a['name']] = a['_id']
    except Exception:
        pass

    return analyzers


def _run_analyzers(ioc, analyzers, data_type, message=None):
    if not message:
        message = 'Manual submit'
    headers = {'Authorization': 'Bearer %s' % CORTEX_API}
    success = 0
    try:
        j_data = {"data":ioc, "dataType": data_type, "tlp":0, "message": message }
        for analyzer in analyzers:
            r = requests.post('%s/api/analyzer/%s/run?force=1' % (CORTEX, analyzer),
                              headers=headers,
                              json=j_data,
                              verify=False)
            analyzer_response = r.json()
            if 'errors' not in analyzer_response:
                success += 1
    except Exception:
        pass

    return success


def _run_file_analyzers(file_path, analyzers):
    headers = {'Authorization': 'Bearer %s' % CORTEX_API}
    success = 0
    try:
        j_data = json.dumps({"dataType": "file", "tlp":0})
        f_data = {'attachment': open(file_path, 'rb'), '_json': (None, j_data, 'application/json')}
        for analyzer in analyzers:
            r = requests.post('%s/api/analyzer/%s/run?force=1' % (CORTEX, analyzer),
                              headers=headers,
                              files=f_data,
                              verify=False)
            analyzer_response = r.json()
            if 'errors' not in analyzer_response:
                success += 1
    except Exception:
        pass

    return success


def _get_jobs(observable=None):
    jobs = {}
    headers = {'Authorization': 'Bearer %s' % CORTEX_API}
    try:
        if observable:
            q = {"query": { "_field": "data", "_value": observable}}
            q2 = {"query": {'_field': 'attachment.name', '_value': observable}}
            r = requests.post('%s/api/job/_search?range=all' % CORTEX, headers=headers, json=q, verify=False)
            r2 = requests.post('%s/api/job/_search?range=all' % CORTEX, headers=headers, json=q2, verify=False)
            jobs = r.json() + r2.json()
        else:
            r = requests.post('%s/api/job/_search?range=all' % CORTEX,
                              headers=headers,
                              data = {'range':'all'},
                              verify=False)
            jobs = r.json()
    except Exception:
        print 'hit exception'
        pass
    return jobs


def _get_job_detail(job_id):
    job = {}
    headers = {'Authorization': 'Bearer %s' % CORTEX_API}
    try:
        r = requests.get('%s/api/job/%s/report' % (CORTEX, job_id), headers=headers, verify=False)
        job = r.json()
    except Exception:
        print 'hit exception'
        pass
    return job


@app.template_filter('urlencode')
def urlencode_filter(s):
    if type(s) == 'Markup':
        s = s.unescape()
    s = s.encode('utf8')
    s = urllib.quote_plus(s)
    return Markup(s)


@app.route('/', methods=['GET', 'POST'])
@app.route('/index', methods=['GET', 'POST'])
def index():
    jobs = _get_jobs()

    summary = {}
    for job in jobs:
        if not job['status'] == 'Success':
            continue
        jdt = datetime.fromtimestamp(job['date'] / 1000)
        ioc = job.get('data') or job['attachment']['name']
        if ioc in summary:
            summary[ioc]['count'] += 1
            if job['message'] not in summary[ioc]['messages']:
                summary[ioc]['messages'].append(job['message'])
            if jdt < summary[ioc]['first_seen']:
                summary[ioc]['first_seen'] = jdt
            if jdt > summary[ioc]['last_seen']:
                summary[ioc]['last_seen'] = jdt
        else:
            summary[ioc] = {'first_seen': jdt, 'last_seen': jdt, 'count': 1, 'messages': [job['message']]}

    order = sorted(summary.keys(), key=lambda x: (summary[x]['last_seen']))

    return render_template('index.html',
                           title='Observable Reports',
                           summary=summary,
                           order=reversed(order))


@app.route('/reports', methods=['GET', 'POST'])
def reports():
    observable = request.args.get('observable')
    if not _validate_observable(observable):
        abort(401)
    jobs = _get_jobs(observable)
    for job in jobs:
        job['createdAt'] = datetime.fromtimestamp(job['date'] / 1000)
        details = _get_job_detail(job['id'])
        job['text'] = 'n/a'
        job['level'] = 'n/a'

        # populate text and level
        report = details.get('report', {})
        summary = report.get('summary', {})
        taxes = summary.get('taxonomies', [])
        for t in taxes:
            job['text'] = t.get('value', 'n/a')
            job['level'] = t.get('level', 'n/a')

    return render_template('observable_reports.html', title='Observable Reports', jobs=jobs)


@app.route('/artifacts', methods=['GET', 'POST'])
def artifacts():
    observable = request.args.get('observable')
    dl = request.args.get('download')
    if not _validate_observable(observable):
        abort(401)
    jobs = _get_jobs(observable)
    results = []
    for job in jobs:
        det = _get_job_detail(job.get('id', ''))
        job_artifacts = det.get('report', {}).get('artifacts', [])
        if len(job_artifacts) < 1:
            continue

        for a in job_artifacts:
            a['analyzerName'] = job['analyzerName']
            a['date'] = datetime.fromtimestamp(job['date'] / 1000)
            if a['dataType'] == 'ip':
                try:
                    if ipaddress.ip_address(unicode(a['data'])).is_global:
                        results.append(a)
                except:
                    pass
            else:
                results.append(a)
    if dl and dl == 'csv':
        output = io.BytesIO()
        keys = results[0].keys()
        dict_writer = csv.DictWriter(output, keys)
        dict_writer.writeheader()
        dict_writer.writerows(results)

        return Response(output.getvalue(),
                        mimetype="text/csv",
                        headers={"Content-disposition": "attachment; filename=output.csv"})

    return render_template('artifacts.html',
                           title='Artifacts From Analysis: %s' % observable,
                           artifacts=results, observable=observable)


@app.route('/query/<data_type>', methods=['GET', 'POST'])
def query(data_type):
    if not (data_type in DATA_TYPES):
        abort(401)
    if data_type == 'file':
        form = FileForm()
    else:
        form = IocForm()

    form.data_type = data_type
    analyzers = _get_analyzers(data_type)
    form.analyzers.choices = [(v, k) for k,v in analyzers.iteritems()]

    if form.validate_on_submit():
        picked_analyzers = form.analyzers.data
        if data_type == 'file':
            fname = secure_filename(form.value.data.filename)
            form.value.data.save('/tmp/' + fname)
            file_path = '/tmp/' + fname
            results = _run_file_analyzers(file_path, picked_analyzers)
        else:
            ioc = form.value.data
            message = form.message.data
            results = _run_analyzers(ioc, picked_analyzers, data_type, message)

        flash('Successfully submitted %s analyzers' % results)

    return render_template('query.html', title='Edit/View Referers', form=form)


@app.route('/query_all', methods=['GET', 'POST'])
def query_all():
    if request.method == "POST":
        data_type = request.json.get('data_type')
        observable = request.json.get('observable')
        orig_observable = request.json.get('pivot', '')

        if not data_type or not observable:
            abort(401)
        if not (data_type in DATA_TYPES):
            abort(401)
        if not _validate_observable(observable):
            abort(401)
        analyzers = list([v for k,v in _get_analyzers(data_type).iteritems()])
        message = 'Auto-pivot on %s' % orig_observable
        results = _run_analyzers(observable, analyzers, data_type, message)

    return jsonify({'success': results})


@app.route('/detail/<job_id>', methods=['GET', 'POST'])
def detail(job_id):
    job = _get_job_detail(job_id)
    pprint.pprint(job)

    return jsonify({'data': job})


@app.route('/download/<file_hash>')
def download(file_hash):
    headers = {'Authorization': 'Bearer %s' % CORTEX_API}
    try:
        r = requests.get('%s/api/datastorezip/%s' % (CORTEX, file_hash),
                         headers=headers,
                         verify=False,
                         stream=True)
    except Exception:
        print 'hit exception'
        return Response("Error Downloading")

    return send_file(io.BytesIO(r.content),
                     attachment_filename='%s.zip' % file_hash,
                     mimetype='application/zip')
