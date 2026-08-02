import time
from datetime import datetime

from sqlalchemy import desc, or_

from .setup import *


class ModelKakaopageItem(ModelBase):
    P = P
    __tablename__ = 'kakaopage_dl_item'
    __table_args__ = {'mysql_collate': 'utf8_general_ci'}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    created_time = db.Column(db.DateTime)
    updated_time = db.Column(db.DateTime)

    series_id = db.Column(db.Integer, index=True)
    series_title = db.Column(db.String)
    product_id = db.Column(db.Integer, index=True, unique=True)
    episode_no = db.Column(db.Integer)
    episode_title = db.Column(db.String)
    page_count = db.Column(db.Integer)

    status = db.Column(db.String, index=True)
    error_msg = db.Column(db.String)

    ticket_uid = db.Column(db.String)
    rent_expire_dt = db.Column(db.DateTime)

    save_dir = db.Column(db.String)
    downloaded_count = db.Column(db.Integer)
    total_bytes = db.Column(db.BigInteger)
    downloaded_at = db.Column(db.DateTime)

    def __init__(self):
        self.created_time = datetime.now()
        self.updated_time = self.created_time
        self.status = 'pending'
        self.downloaded_count = 0
        self.total_bytes = 0

    @classmethod
    def make_query(cls, req, order='desc', search='', option1='all', option2='all'):
        query = db.session.query(cls)
        opt = (req.form.get('option') or option1 or 'all').strip()
        kw = (req.form.get('search_word') or req.form.get('keyword') or search or '').strip()
        kind = (req.form.get('kind_filter') or 'all').strip()
        if opt and opt != 'all':
            query = query.filter(cls.status == opt)
        if kw:
            pat = f'%{kw}%'
            query = query.filter(or_(cls.series_title.like(pat),
                                     cls.episode_title.like(pat)))
        if kind == 'novel':
            query = query.filter(or_(cls.save_dir.like('%/novel/%'),
                                     cls.save_dir.like('%\\novel\\%')))
        elif kind == 'comic':
            query = query.filter(or_(cls.save_dir.like('%/webtoon/%'),
                                     cls.save_dir.like('%\\webtoon\\%')))
        if order == 'desc':
            query = query.order_by(desc(cls.id))
        else:
            query = query.order_by(cls.id)
        return query

    # ------------------------------------------------------------------
    # 이력 조회 최적화 (web_list 오버라이드)
    #
    # 프레임워크 기본 web_list 는 요청마다
    #   (1) query.count()  — 필터 조건에 인덱스가 없으면 전체 스캔
    #   (2) ModelSetting.set(..._last_list_option)  — 쓰기 트랜잭션 1회
    # 를 수행한다. 같은 SQLite 파일에 내부 부가 테이블이 함께 들어 있어서
    # 다운로드 중에는 이 두 가지가 디스크/쓰기락 경합에 그대로 걸린다.
    # → count 는 짧게 캐시하고, 옵션 저장은 값이 바뀔 때만 쓴다.
    # ------------------------------------------------------------------
    _count_cache = {}          # sig -> (count, expire_ts)
    _count_ttl = 30            # 초
    _last_list_option_cache = None

    @classmethod
    def _cached_count(cls, query, sig):
        now = time.time()
        hit = cls._count_cache.get(sig)
        if hit and hit[1] > now:
            return hit[0]
        count = query.count()
        if len(cls._count_cache) > 50:
            cls._count_cache.clear()
        cls._count_cache[sig] = (count, now + cls._count_ttl)
        return count

    @classmethod
    def invalidate_count_cache(cls):
        cls._count_cache.clear()

    @classmethod
    def web_list(cls, req):
        try:
            ret = {}
            page = int(req.form.get('page', 1) or 1)
            page_size = 30
            search = (req.form.get('keyword') or '').strip()
            option1 = req.form.get('option1', 'all')
            option2 = req.form.get('option2', 'all')
            order = req.form.get('order', 'desc')

            query = cls.make_query(req, order=order, search=search,
                                   option1=option1, option2=option2)
            sig = '|'.join([
                (req.form.get('option') or option1 or 'all').strip(),
                (req.form.get('search_word') or req.form.get('keyword')
                 or search or '').strip(),
                (req.form.get('kind_filter') or 'all').strip(),
            ])
            count = cls._cached_count(query, sig)
            lists = query.limit(page_size).offset((page - 1) * page_size).all()
            ret['list'] = [item.as_dict() for item in lists]
            ret['paging'] = cls.get_paging_info(count, page, page_size)

            opt = f'{order}|{page}|{search}|{option1}|{option2}'
            if opt != cls._last_list_option_cache:
                cls._last_list_option_cache = opt
                try:
                    P.ModelSetting.set(f'{cls.__tablename__}_last_list_option', opt)
                except Exception:
                    pass
            return ret
        except Exception as e:
            P.logger.error(f'Exception:{str(e)}')
            import traceback
            P.logger.error(traceback.format_exc())
