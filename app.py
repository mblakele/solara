from datetime import datetime, timedelta
from decouple import config
from flask import Flask, Response
from flask import abort, make_response, render_template, request
from flask.json.provider import DefaultJSONProvider
import humps
import isodate
import logging
import pytz

from metrics import Metrics, MetricsMock, RetryableMetricsException

# global setup
app = Flask(__name__)
# The template_folder and static_folder default to 'templates' and 'static'
# relative to the application path. Using the default root structure.
DEBUG = config('DEBUG', False, cast=bool)
logger = app.logger
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)

def astimezone_filter(dt, tz_str):
    tz = pytz.timezone(tz_str)
    return dt.astimezone(tz)

app.jinja_env.filters['astimezonestr'] = astimezone_filter


class CustomJSONProvider(DefaultJSONProvider):

    def default(self, o):
        try:
            if isinstance(o, datetime):
                return o.isoformat()
            if isinstance(o, timedelta):
                return isodate.duration_isoformat(o)
            iterable = iter(o)
        except TypeError:
            pass
        else:
            return list(iterable)
        return super().default(o)

app.json = CustomJSONProvider(app)


@app.errorhandler(RetryableMetricsException)
def error_retryable(e):
    resp = make_response(
        render_template('error_retryable.html', exception=e),
        500)
    resp.headers['Refresh'] = '5'
    return resp

@app.route('/')
def index():
    logger.debug('index')
    is_mock = config('VUE_USERNAME', None) is None
    if config('MOCK', default='False', cast=bool):
        is_mock = True
    is_mock_error = config('MOCK_ERROR', default='False', cast=bool)

    model = None
    if is_mock_error:
        raise RetryableMetricsException('mock')
    elif is_mock:
        model = MetricsMock()
    else:
        model = Metrics(logger)

    # check for default html first, to handle missing Accept header.
    if request.accept_mimetypes.accept_html:
        return render_template('index.html', metrics=model.metrics)

    if request.accept_mimetypes.accept_json:
        resp = Response(app.json.dumps(humps.camelize(model.metrics)))
        resp.headers['Content-Type'] = 'application/json'
        return resp

    # content negotiation failed
    return abort(406)

@app.route('/health')
def health():
    logger.debug('health')
    resp = Response('ok')
    resp.headers['Content-Type'] = 'text/plain'
    return resp

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
