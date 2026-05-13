from datetime import datetime

from .setup import *


class ModelKakaopageItem(ModelBase):
    P = P
    __tablename__ = 'kakaopage_dl_item'
    __table_args__ = {'mysql_collate': 'utf8_general_ci'}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    created_time = db.Column(db.DateTime)
    updated_time = db.Column(db.DateTime)

    # 작품/회차
    series_id = db.Column(db.Integer, index=True)
    series_title = db.Column(db.String)
    product_id = db.Column(db.Integer, index=True, unique=True)
    episode_no = db.Column(db.Integer)        # 6 (회차 번호)
    episode_title = db.Column(db.String)      # "늙은 죄수는 고독에 산다 6화"
    page_count = db.Column(db.Integer)        # 응답의 totalCount

    # 처리 상태: pending / using_ticket / downloading / completed / failed / skipped_no_ticket / skipped_paid_only
    status = db.Column(db.String, index=True)
    error_msg = db.Column(db.String)

    # 카카오 차감 결과
    ticket_uid = db.Column(db.String)
    rent_expire_dt = db.Column(db.DateTime)

    # 파일 저장
    save_dir = db.Column(db.String)            # 회차 폴더
    downloaded_count = db.Column(db.Integer)   # 실제 받은 이미지 수
    total_bytes = db.Column(db.BigInteger)     # 받은 총 바이트
    downloaded_at = db.Column(db.DateTime)

    def __init__(self):
        self.created_time = datetime.now()
        self.updated_time = self.created_time
        self.status = 'pending'
        self.downloaded_count = 0
        self.total_bytes = 0
