import os

import logbook
from elasticapm.contrib.flask import ElasticAPM
from elasticapm.handlers.logbook import LogbookHandler as ElasticHandler
from flask import Flask, g, render_template, session
from flask_cdn import CDN
from htmlmin import minify
from raven.contrib.flask import Sentry
from raven.handlers.logbook import SentryHandler

from everyclass.server.db.mysql import get_local_conn, init_pool
from everyclass.server.utils import monkey_patch

logger = logbook.Logger(__name__)

sentry = Sentry()

ElasticAPM.request_finished = monkey_patch.ElasticAPM.request_finished(ElasticAPM.request_finished)


def create_app(offline=False) -> Flask:
    """创建 flask app
    @param offline: 如果设置为 `True`，则为离线模式。此模式下不会连接到 Sentry 和 ElasticAPM
    """
    app = Flask(__name__,
                static_folder='../../frontend/dist',
                static_url_path='',
                template_folder="../../frontend/templates")

    # load app config
    from everyclass.server.config import get_config
    _config = get_config()
    app.config.from_object(_config)

    # CDN
    CDN(app)

    # logbook handlers
    # 规则如下：
    # - 全部输出到 stdout（本地开发调试、服务器端文件日志）
    # - Elastic APM 或者 LogStash（日志中心）
    # - WARNING 以上级别的输出到 Sentry
    #
    # 日志等级：
    # critical – for errors that lead to termination
    # error – for errors that occur, but are handled
    # warning – for exceptional circumstances that might not be errors
    # notice – for non-error messages you usually want to see
    # info – for messages you usually don’t want to see
    # debug – for debug messages
    #
    # Elastic APM：
    # log.info("Nothing to see here", stack=False)
    # stack 默认是 True，设置为 False 将不会向 Elastic APM 发送 stack trace
    # https://discuss.elastic.co/t/how-to-use-logbook-handler/146209/6
    #
    # Sentry：
    # https://docs.sentry.io/clients/python/api/#raven.Client.captureMessage
    # - stack 默认是 False，和 Elastic APM 的不一致，所以还是每次手动指定吧...
    # - 默认事件类型是 `raven.events.Message`，设置 `exc_info` 为 `True` 将把事件类型升级为`raven.events.Exception`
    stderr_handler = logbook.StderrHandler(bubble=True)
    logger.handlers.append(stderr_handler)

    if not offline:
        # Sentry
        sentry.init_app(app=app)
        sentry_handler = SentryHandler(sentry.client, level='WARNING')  # Sentry 只处理 WARNING 以上的
        logger.handlers.append(sentry_handler)

        # Elastic APM
        apm = ElasticAPM(app)
        elastic_handler = ElasticHandler(client=apm.client, bubble=True)
        logger.handlers.append(elastic_handler)

    # 初始化数据库
    if os.getenv('MODE', None) != "CI":
        init_pool(app)

    # 导入并注册 blueprints
    from everyclass.server.calendar.views import cal_blueprint
    from everyclass.server.query import query_blueprint
    from everyclass.server.views import main_blueprint as main_blueprint
    from everyclass.server.api import api_v1 as api_blueprint
    app.register_blueprint(cal_blueprint)
    app.register_blueprint(query_blueprint)
    app.register_blueprint(main_blueprint)
    app.register_blueprint(api_blueprint, url_prefix='/api/v1')

    @app.before_request
    def set_user_id():
        """在请求之前设置 session uid，方便 Elastic APM 记录用户请求"""
        if not session.get('user_id', None):
            # 数据库中生成唯一 ID，参考 https://blog.csdn.net/longjef/article/details/53117354
            conn = get_local_conn()
            cursor = conn.cursor()
            cursor.execute("REPLACE INTO user_id_sequence (stub) VALUES ('a');")
            session['user_id'] = cursor.lastrowid
            cursor.close()

    @app.teardown_request
    def close_db(error):
        """结束时关闭数据库连接"""
        if hasattr(g, 'mysql_db') and g.mysql_db:
            g.mysql_db.close()

    @app.after_request
    def response_minify(response):
        """用 htmlmin 压缩 HTML，减轻带宽压力"""
        if app.config['HTML_MINIFY'] and response.content_type == u'text/html; charset=utf-8':
            response.set_data(minify(response.get_data(as_text=True)))
        return response

    @app.template_filter('versioned')
    def version_filter(filename):
        """
        模板过滤器。如果 STATIC_VERSIONED，返回类似 'style-v1-c012dr.css' 的文件，而不是 'style-v1.css'

        :param filename: 文件名
        :return: 新的文件名
        """
        if app.config['STATIC_VERSIONED']:
            if filename[:4] == 'css/':
                new_filename = app.config['STATIC_MANIFEST'][filename[4:]]
                return 'css/' + new_filename
            elif filename[:3] == 'js/':
                new_filename = app.config['STATIC_MANIFEST'][filename[3:]]
                return new_filename
            else:
                return app.config['STATIC_MANIFEST'][filename]
        return filename

    @app.errorhandler(500)
    def internal_server_error(error):
        return render_template('500.html',
                               event_id=g.sentry_event_id,
                               public_dsn=sentry.client.get_public_dsn('https'))

    logger.info('App created with `{}` config'.format(app.config['CONFIG_NAME']), stack=False)
    return app
