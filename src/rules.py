from __future__ import annotations
import re
from .domain import ConflictError, ValidationError
TITLE='职业辐射剂量与异常事件'; ENTITY='剂量事件'; ID_PREFIX='RD'
SEVERITIES=['low', 'elevated', 'high', 'critical']; STATES=['recorded', 'reviewing', 'investigation', 'follow_up', 'closed']; TRANSITIONS={'recorded': ['reviewing'], 'reviewing': ['investigation'], 'investigation': ['follow_up'], 'follow_up': ['closed'], 'closed': []}; TRANSITION_ROLES={'reviewing': ['radiation_officer'], 'investigation': ['radiation_officer'], 'follow_up': ['health_physicist'], 'closed': ['health_physicist']}
CREATE_ROLES=set(['dosimetrist']); RECORD_ROLES=set(['radiation_officer', 'health_physicist']); AUDIT_ROLES=set(['health_physicist', 'viewer']); VIEW_ROLES=set(['dosimetrist', 'radiation_officer', 'health_physicist', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'elevated': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'elevated': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))

# 月度归集
PERSON_ENTITY='受照人员'; PERIOD_ENTITY='监测周期'; READING_ENTITY='剂量读数'; SUMMARY_ENTITY='归集结果'
PERIOD_STATUSES=['open','sealed']; SUMMARY_STATUSES=['draft','confirmed']
PERSON_CREATE_ROLES=set(['dosimetrist','radiation_officer']); READING_ROLES=set(['dosimetrist']); COLLECT_ROLES=set(['dosimetrist','radiation_officer']); SEAL_ROLES=set(['radiation_officer']); CONFIRM_ROLES=set(['radiation_officer'])
INVESTIGATION_LEVEL_MSV=2.0; INVESTIGATION_JUMP_RATIO=3.0; DOSE_ROUND=6
PERIOD_RE=re.compile(r'^(\d{4})-(\d{2})$')

def normalize_period(value):
    if not isinstance(value,str) or not PERIOD_RE.match(value.strip()): raise ValidationError("period格式必须为YYYY-MM")
    period=value.strip(); year,month=map(int,PERIOD_RE.match(period).groups())
    if not 1<=month<=12: raise ValidationError("period月份必须在01到12之间")
    return period

def previous_period(period):
    year,month=map(int,period.split('-'))
    month-=1
    if month==0: month=12; year-=1
    return f"{year:04d}-{month:02d}"

def aggregate_by_source(readings):
    # 只取未被更正取代的读数；同一来源读数相加（重复上报会被唯一约束拦截，此处为防御性求和）
    active=[r for r in readings if r["superseded_by"] is None]
    buckets={}
    for r in active:
        b=buckets.setdefault(r["source"], {"source": r["source"], "value": 0.0, "reading_ids": []})
        b["value"]=round(b["value"]+r["value"], DOSE_ROUND); b["reading_ids"].append(r["id"])
    return [buckets[k] for k in sorted(buckets)]

def total_from_sources(sources):
    return round(sum(s["value"] for s in sources), DOSE_ROUND)

def investigation_decision(total, previous_total):
    reasons=[]
    if total>=INVESTIGATION_LEVEL_MSV:
        reasons.append(f"月剂量{total}mSv达到调查水平{INVESTIGATION_LEVEL_MSV}mSv")
    if previous_total is not None and previous_total>0 and total>=previous_total*INVESTIGATION_JUMP_RATIO:
        reasons.append("较上一周期增长达到3倍")
    return bool(reasons), reasons
