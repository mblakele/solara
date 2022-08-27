from datetime import datetime, timedelta
from decouple import config
from flask import Flask, Response, abort, render_template, request
from flask.json import JSONEncoder

import isodate
import json
import logging
import pytz

from metrics import Metrics, MetricsMock, RetryableMetricsException

import metrics

# global setup
app = Flask(__name__)
DEBUG = config('DEBUG', False, cast=bool)
logger = app.logger
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)

def astimezone_filter(dt, tz_str):
    tz = pytz.timezone(tz_str)
    return dt.astimezone(tz)

app.jinja_env.filters['astimezonestr'] = astimezone_filter

class JSONEncoderSolara(JSONEncoder):
    def default(self, obj):
        try:
            if isinstance(obj, datetime):
                return obj.isoformat()
            if isinstance(obj, timedelta):
                return isodate.duration_isoformat(obj)
            iterable = iter(obj)
        except TypeError:
            pass
        else:
            return list(iterable)
        return JSONEncoder.default(self, obj)

app.json_encoder = JSONEncoderSolara

@app.route('/')
def index():
    logger.info('index')
    # TODO retry loop? Not clearly useful
    is_mock = config('VUE_USERNAME', None) is None
    # TODO handle requests.exceptions.HTTPError
    model = MetricsMock() if is_mock else Metrics(logger)

    # check for default html first, to handle missing Accept header.
    if request.accept_mimetypes.accept_html:
        return render_template('index.html', metrics=model.metrics)

    if request.accept_mimetypes.accept_json:
        resp = Response(app.json.dumps(model.metrics))
        resp.headers['Content-Type'] = 'application/json'
        return resp

    # content negotiation failed
    return abort(406)

# end
