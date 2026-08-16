"""
CTP SimNow 

 SimNow ：
- ：（），3
- ：7x24 ， 16:00~09:00， 16:00~12:00

：
    from bt_api_py.ctp_env_selector import get_ctp_fronts
    td_front, md_front, env_name = get_ctp_fronts()
"""

from __future__ import annotations

import os
from datetime import datetime, time

# ──  ( UTC+8) ──────────────────────────
# ：
#   : 09:00-11:30, 13:30-15:00
#   : 21:00-02:30 ( 01:00  23:00)
# ，

_TRADING_SESSIONS = [
    (time(9, 0), time(11, 30)),
    (time(13, 30), time(15, 0)),
    (time(21, 0), time(23, 59, 59)),
]

#  00:00 ~ 02:30
_NIGHT_SESSION_AFTER_MIDNIGHT = (time(0, 0), time(2, 30))

# ：
#   : 16:00 ~  09:00
#   : 16:00 ~  12:00
# : 


def _is_weekday(dt: datetime) -> bool:
    """ (~)"""
    return dt.weekday() < 5


def _in_trading_session(now: datetime) -> bool:
    """"""
    t = now.time()

    #  00:00~02:30 — 
    if _NIGHT_SESSION_AFTER_MIDNIGHT[0] <= t <= _NIGHT_SESSION_AFTER_MIDNIGHT[1]:
        return True

    #  +  21:00~23:59
    return any(start <= t <= end for start, end in _TRADING_SESSIONS)


def _is_set1_available(now: datetime) -> bool:
    """（）"""
    t = now.time()

    #  00:00~02:30: 
    if _NIGHT_SESSION_AFTER_MIDNIGHT[0] <= t <= _NIGHT_SESSION_AFTER_MIDNIGHT[1]:
        #  (weekday=5) 
        prev_day_weekday = (now.weekday() - 1) % 7
        return prev_day_weekday < 5  # 

    #  +  21:00 
    if not _is_weekday(now):
        return False

    return _in_trading_session(now)


def get_ctp_fronts(
    env: str = '',
    now: datetime | None = None,
) -> tuple[str, str, str]:
    """
     CTP 。

    Parameters
    ----------
    env : str
        : "auto" / "set1" / "set2"。
         CTP_ENV ， "auto"。
    now : datetime, optional
        ， datetime.now()。。

    Returns
    -------
    (td_front, md_front, env_name) : tuple[str, str, str]
        、、("set1_groupN"  "set2_7x24")
    """
    if now is None:
        now = datetime.now()

    if not env:
        env = os.environ.get('CTP_ENV', 'auto').strip().lower()

    if env == 'set1':
        return _get_set1_fronts()
    elif env == 'set2':
        return _get_set2_fronts()
    else:
        # auto 
        if _is_set1_available(now):
            return _get_set1_fronts()
        else: return _get_set2_fronts()


def _get_set1_fronts() -> tuple[str, str, str]:
    """（ CTP_SET1_GROUP ）"""
    group = os.environ.get('CTP_SET1_GROUP', '1').strip()
    td = os.environ.get(f'CTP_SET1_TD_FRONT_{group}', 'tcp://182.254.243.31:30001')
    md = os.environ.get(f'CTP_SET1_MD_FRONT_{group}', 'tcp://182.254.243.31:30011')
    # 
    os.environ['CTP_TD_FRONT'] = td
    os.environ['CTP_MD_FRONT'] = md
    return td, md, f'set1_group{group}'


def _get_set2_fronts() -> tuple[str, str, str]:
    """ 7x24 """
    td = os.environ.get('CTP_SET2_TD_FRONT', 'tcp://182.254.243.31:40001')
    md = os.environ.get('CTP_SET2_MD_FRONT', 'tcp://182.254.243.31:40011')
    # 
    os.environ['CTP_TD_FRONT'] = td
    os.environ['CTP_MD_FRONT'] = md
    return td, md, 'set2_7x24'


def apply_ctp_env() -> tuple[str, str, str]:
    """
     CTP ， CTP_TD_FRONT / CTP_MD_FRONT 。
     load_dotenv() 。

    Returns
    -------
    (td_front, md_front, env_name) : tuple[str, str, str]
    """
    td, md, name = get_ctp_fronts()
    return td, md, name
