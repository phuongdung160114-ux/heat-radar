from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator, ConfigDict
from .common import parse_time, number, uid

class Record(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['snapshot','participation','content','coverage','quote','forecast','claim']
    record_id: str = ''
    source: str = Field(min_length=1,max_length=120)
    family: str = Field(default='',max_length=120)
    panel_id: str = 'fixed-v1'
    objects: dict[str,Literal['explicit','context']] = Field(default_factory=dict)
    occurred_at: str
    observed_at: str
    entity_id: str | None = None
    entity_type: str = 'account'
    metric: str = ''
    value: float | None = None
    unit: str = ''
    window_kind: str = 'snapshot'
    pool_version: str = 'v1'
    title: str = ''
    text: str = ''
    url: str = ''
    extra: dict[str,Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def check(self):
        parse_time(self.occurred_at); parse_time(self.observed_at)
        if self.value is not None and number(self.value) is None: raise ValueError('无效数值')
        if not self.family: self.family=self.source
        if self.kind=='participation' and (not self.entity_id or self.entity_id in ['[deleted]','[removed]']):
            raise ValueError('参与者必须有真实的、平台内稳定的ID；不能用阅读量合成账号')
        if len(self.objects)>1000: raise ValueError('单条对象数量过大')
        if not self.record_id:
            self.record_id=uid(self.kind,self.source,self.panel_id,self.objects,self.occurred_at,
                               self.metric,self.entity_id,self.title,self.text,self.value,self.extra)
        return self

class IngestBatch(BaseModel):
    records: list[Record] = Field(max_length=50000)
    preserve_observed_at: bool = False

class RunRequest(BaseModel):
    phase: Literal['auto','premarket','intraday','postmarket'] = 'auto'
    as_of: str | None = None

class BackfillRequest(BaseModel):
    days: int = Field(default=25,ge=1,le=260)
    symbols: list[str] = Field(default_factory=list,max_length=30)
