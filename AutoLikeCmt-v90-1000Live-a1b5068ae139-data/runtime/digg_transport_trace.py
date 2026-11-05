"""Completed HTTP metadata only; never consume SDK streams or expose credentials."""
import base64,json,time,os
import threading
from pathlib import Path
from collections import Counter
from urllib.parse import urlsplit,parse_qs
_file_lock=threading.Lock()

def report(bot, rows):
    # Full metadata stays out of the UI's truncated lines and rendering queue.
    try:
        folder=Path(__file__).resolve().parent/'logs'
        folder.mkdir(exist_ok=True)
        path=folder/'digg-http.jsonl'
        with _file_lock:
            if path.exists() and path.stat().st_size>10*1024*1024:
                path.replace(folder/'digg-http.previous.jsonl')
            with path.open('a',encoding='utf-8') as stream:
                for row in rows:
                    account=str(getattr(bot,'last_login_username','') or getattr(bot,'prefix','')).strip('[] @')
                    stream.write(json.dumps(dict(row,logged_at=time.time(),account=account),ensure_ascii=True)+'\n')
    except Exception as error:
        bot.log('[digg-http] Detail file error: '+type(error).__name__)
    counts=Counter(row.get('outcome','unconfirmed') for row in rows)
    bot.log('[digg-http] '+', '.join(f'{key}={value}' for key,value in sorted(counts.items())))
    # Representative failures retain ID, code and timing within the UI limit.
    failures=[r for r in rows if r.get('outcome') not in ('api_success','preflight','csrf_handshake')]
    for row in failures[:2]:
        compact={k:v for k,v in row.items() if k not in ('parameter_keys','json_keys','encoded_bytes')}
        bot.log('[digg-http] '+json.dumps(compact,ensure_ascii=True,separators=(',',':')))

def describe_body(content):
    if not isinstance(content, dict):
        return {'body_bytes': 0, 'outcome': 'unconfirmed', 'body_kind': 'unavailable'}
    body=content.get('body','') or ''
    if content.get('base64Encoded'): body=base64.b64decode(body,validate=True).decode('utf-8')
    row={'body_bytes':len(body.encode('utf-8')), 'outcome':'unconfirmed'}
    if not body.strip(): return dict(row,body_kind='empty')
    try: data=json.loads(body)
    except ValueError: return dict(row,body_kind='non_json',parse_error='invalid_json')
    row['body_kind']='json'
    if isinstance(data,dict):
        row['json_keys']=[k for k in data if isinstance(k,str) and k.isidentifier()][:30]
        codes=[]; invalid=False
        for key in ('status_code','statusCode'):
            if key not in data: continue
            code=data[key]
            if isinstance(code,int) and not isinstance(code,bool) and code>=0: codes.append(code)
            elif isinstance(code,str) and code.isascii() and code.isdigit(): codes.append(int(code))
            else: invalid=True
        if not invalid and codes and len(set(codes))==1:
            row['server_code']=codes[0]
            row['outcome']='api_success' if codes[0]==0 else 'business_failure'
        elif codes or invalid: row['status_error']='invalid_or_conflicting_status'
        message=str(data.get('status_msg') or data.get('message') or '').lower()
        row['server_reason']=next((label for token,label in [('captcha','captcha'),('login','login'),('frequen','rate_limit'),('too many','rate_limit'),('forbidden','forbidden'),('signature','signature'),('verify','verification')] if token in message),'unspecified')
    return row

def collect(driver,wait_seconds=0.02,skip_bodies=False):
    if driver is None:
        return []
    requests={}; deadline=time.monotonic()+wait_seconds
    while True:
        for entry in (driver.get_log('performance') or []):
            raw_msg = entry.get('message') if isinstance(entry, dict) else None
            if not isinstance(raw_msg, str):
                continue
            if not requests and '/api/comment/digg/' not in raw_msg:
                continue
            if requests and not (
                '/api/comment/digg/' in raw_msg
                or 'Network.responseReceived"' in raw_msg
                or 'Network.loadingFinished"' in raw_msg
                or 'Network.loadingFailed"' in raw_msg
            ):
                continue
            try: event=json.loads(raw_msg)['message']
            except (KeyError,TypeError,ValueError): continue
            if not isinstance(event, dict): continue
            method=event.get('method'); params=event.get('params') or {}; rid=params.get('requestId')
            if method=='Network.requestWillBeSent':
                request=params.get('request') or {}; url=urlsplit(str(request.get('url','')))
                if url.path!='/api/comment/digg/': continue
                payload=request.get('postData') or ''
                try: fields=list(json.loads(payload)) if payload else []
                except (ValueError,TypeError): fields=list(parse_qs(payload))
                wire=dict(parse_qs(url.query)); wire.update(parse_qs(payload))
                public_fields={k:str(v[0]) for k,v in wire.items() if k in ('cid','aweme_id','item_id','comment_id','digg_type','type') and v and str(v[0]).isdigit()}
                header_names=sorted(str(k).lower() for k in (request.get('headers') or {}))
                fields=sorted({k for k in fields if isinstance(k,str) and k.isidentifier()}|{k for k in parse_qs(url.query) if k.isidentifier()})
                requests[rid]={'request_id':rid,'endpoint':url.path,'method':request.get('method','unknown'),'parameter_keys':fields,'wire_fields':public_fields,'header_names':header_names,'_start':params.get('timestamp'),'_finished':False}
            elif rid in requests:
                row=requests[rid]
                if method=='Network.responseReceived':
                    response=params.get('response') or {}; row.update(http=response.get('status'),mime=response.get('mimeType','')); row['_response']=params.get('timestamp')
                elif method=='Network.loadingFinished':
                    row['_finished']=True; row['_end']=params.get('timestamp'); row['encoded_bytes']=params.get('encodedDataLength')
                elif method=='Network.loadingFailed':
                    row['_finished']=True; row['_end']=params.get('timestamp'); error=str(params.get('errorText',''))
                    row.update(network_error=error if error.startswith('net::ERR_') and len(error)<80 else 'network_failure',outcome='transport_failure')
        if not requests or all(r['_finished'] for r in requests.values()) or time.monotonic()>=deadline: break
        time.sleep(0.01)
    results=[]
    for rid,row in requests.items():
        if row.get('method')=='OPTIONS': row.update(body_kind='preflight',outcome='preflight')
        elif row.get('method')=='HEAD' and 200 <= int(row.get('http',0)) < 400: row.update(body_kind='no_body_expected',outcome='csrf_handshake')
        elif 'network_error' not in row:
            if not row['_finished']: row.update(body_kind='unavailable',outcome='unconfirmed',body_error='loading_not_finished')
            elif skip_bodies and isinstance(row.get('http'),(int,float)) and 200<=row['http']<300:
                row.update(body_kind='not_read',outcome='http_success_unverified')
            else:
                try: row.update(describe_body(driver.execute_cdp_cmd('Network.getResponseBody',{'requestId':rid})))
                except Exception as error: row.update(body_kind='unavailable',outcome='unconfirmed',body_error=type(error).__name__)
            if isinstance(row.get('http'),(int,float)) and not 200<=row['http']<300: row['outcome']='http_failure'
        start=row.get('_start')
        for name,key in [('response_ms','_response'),('duration_ms','_end')]:
            if isinstance(start,(int,float)) and isinstance(row.get(key),(int,float)): row[name]=round(max(0,row[key]-start)*1000,1)
        results.append({k:v for k,v in row.items() if not k.startswith('_')})
    return results

def install():
    import tiktok_reply as tt
    cls=tt.TikTokLikeBot
    if getattr(cls,'_digg_transport_trace',False): return
    original=cls.api_digg_comments
    def digg(self,*args,**kwargs):
        if os.environ.get('ALC_HTTP_TRACE') != '1':
            return original(self,*args,**kwargs)
        self.last_digg_verification={}
        if getattr(self, 'driver', None) is not None:
            try: self.driver.get_log('performance')
            except Exception: pass
        result=original(self,*args,**kwargs)
        if getattr(self, 'driver', None) is None or self.stop_event.is_set():
            return result
        try:
            verified=getattr(self,'last_digg_verification',{})
            all_confirmed=bool(verified.get('requested') and verified.get('confirmed')==verified['requested'])
            rows=collect(self.driver); self.last_digg_transport=rows
            if rows: report(self,rows)
            if not rows:
                if all_confirmed:
                    self.log('[digg-http] Không gửi tim mới: các CID đã có trạng thái cần thiết trên server.')
                else:
                    self.log('[digg-http] No captured request; outcome unconfirmed.')
        except Exception as error: self.log('[digg-http] Capture error: '+type(error).__name__)
        return result
    cls.api_digg_comments=digg; cls._digg_transport_trace=True
