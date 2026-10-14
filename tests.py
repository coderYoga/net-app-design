#!/usr/bin/env python3
import json, os, subprocess, sys, tempfile, time, urllib.request, urllib.error
ROOT=os.path.dirname(__file__); port=18081; env={**os.environ,'SMARTSHOP_DB':tempfile.mktemp(suffix='.db')}; proc=subprocess.Popen([sys.executable,os.path.join(ROOT,'app.py'),'--host','127.0.0.1','--port',str(port)],env=env,stdout=subprocess.DEVNULL)
def call(path,method='GET',body=None,token=None):
    data=json.dumps(body).encode() if body is not None else None;h={'Content-Type':'application/json'}
    if token:h['Authorization']='Bearer '+token
    r=urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{port}/api'+path,data=data,headers=h,method=method));return r.status,json.loads(r.read())
try:
    for _ in range(30):
        try:call('/health');break
        except Exception:time.sleep(.1)
    checks=[]
    checks.append(('健康检查',call('/health')[1]['status']=='ok'))
    checks.append(('游客商品浏览',len(call('/products')[1])==12))
    _,login=call('/login','POST',{'username':'demo','password':'demo123'});ct=login['token'];checks.append(('顾客登录',login['user']['role']=='customer'))
    call('/cart','POST',{'product_id':1,'qty':1},ct);checks.append(('加入购物车',len(call('/cart',token=ct)[1])==1))
    checks.append(('付款结算',call('/checkout','POST',{},ct)[1]['ok']))
    _,sales=call('/login','POST',{'username':'sales01','password':'sales123'});st=sales['token'];checks.append(('销售登录',sales['user']['role']=='sales'))
    a=call('/analytics',token=st)[1];checks.append(('数据分析',all(k in a for k in ('trend','forecast','ranking','profiles','anomalies'))))
    checks.append(('协同过滤推荐',len(call('/recommendations',token=ct)[1])>0))
    _,admin=call('/login','POST',{'username':'admin','password':'admin123'});at=admin['token'];checks.append(('管理员账号列表',len(call('/users/sales',token=at)[1])>=1))
    for name,ok in checks:print(('PASS' if ok else 'FAIL'),name)
    if not all(x[1] for x in checks):sys.exit(1)
finally:proc.terminate();proc.wait(timeout=5)
