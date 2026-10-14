#!/usr/bin/env python3
import argparse, csv, hashlib, hmac, io, json, math, mimetypes, os, secrets, sqlite3, statistics, time
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("SMARTSHOP_DB", ROOT / "data" / "smartshop.db"))
STATIC = ROOT / "static"

def now(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def today(offset=0): return (datetime.now() + timedelta(days=offset)).strftime("%Y-%m-%d")

def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 160000)
    return salt.hex() + "$" + digest.hex()

def password_ok(password, stored):
    try:
        salt, expected = stored.split("$", 1)
        return hmac.compare_digest(password_hash(password, bytes.fromhex(salt)).split("$")[1], expected)
    except Exception: return False

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE, email TEXT, password TEXT, role TEXT, region TEXT, active INTEGER DEFAULT 1, created_at TEXT);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER, expires_at INTEGER, FOREIGN KEY(user_id) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, category_id INTEGER, name TEXT, description TEXT, price REAL, stock INTEGER, emoji TEXT, status TEXT DEFAULT '上架', created_at TEXT, FOREIGN KEY(category_id) REFERENCES categories(id));
        CREATE TABLE IF NOT EXISTS cart_items(user_id INTEGER, product_id INTEGER, qty INTEGER, PRIMARY KEY(user_id,product_id));
        CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, user_id INTEGER, total REAL, status TEXT, created_at TEXT, FOREIGN KEY(user_id) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS order_items(id INTEGER PRIMARY KEY, order_id INTEGER, product_id INTEGER, product_name TEXT, category_id INTEGER, unit_price REAL, qty INTEGER);
        CREATE TABLE IF NOT EXISTS login_logs(id INTEGER PRIMARY KEY, username TEXT, role TEXT, ip TEXT, success INTEGER, created_at TEXT);
        CREATE TABLE IF NOT EXISTS browse_logs(id INTEGER PRIMARY KEY, user_id INTEGER, product_id INTEGER, category_id INTEGER, seconds INTEGER, ip TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS operation_logs(id INTEGER PRIMARY KEY, user_id INTEGER, username TEXT, content TEXT, ip TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS email_logs(id INTEGER PRIMARY KEY, user_id INTEGER, subject TEXT, content TEXT, created_at TEXT);
        """)
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            for u,e,p,r,g in [("demo","demo@example.com","demo123","customer","广东"),("alice","alice@example.com","alice123","customer","上海"),("bob","bob@example.com","bob12345","customer","北京"),("sales01","sales@shop.local","sales123","sales","广东"),("admin","admin@shop.local","admin123","admin","广东")]:
                c.execute("INSERT INTO users(username,email,password,role,region,created_at) VALUES(?,?,?,?,?,?)",(u,e,password_hash(p),r,g,now()))
            cats=["数码科技","居家生活","户外运动","图书文创"]
            for x in cats: c.execute("INSERT INTO categories(name) VALUES(?)",(x,))
            products=[(1,"云感降噪耳机","40mm 动圈与主动降噪，通勤好搭档",699,28,"🎧"),(1,"轻薄生产力平板","2.8K 护眼屏，全天候续航",3299,14,"📱"),(1,"机械键盘 Pro","热插拔轴体与三模连接",459,4,"⌨️"),(2,"智能温控杯","长效保温，温度实时显示",189,35,"☕"),(2,"香薰氛围灯","无级调光，静音香薰",239,22,"💡"),(2,"人体工学椅","自适应腰托与多向扶手",1599,8,"🪑"),(3,"城市轻跑鞋","缓震中底，透气织面",529,19,"👟"),(3,"折叠露营椅","铝合金支架，轻量便携",269,3,"🏕️"),(3,"运动手环 S","全天心率与睡眠监测",399,31,"⌚"),(4,"系统设计之道","从需求到可演进架构",88,45,"📘"),(4,"城市建筑拼图","1000 片博物馆级插画",128,17,"🧩"),(4,"手账礼盒","高克重纸与黄铜配件",159,26,"📓")]
            for cat,name,desc,price,stock,emoji in products:
                c.execute("INSERT INTO products(category_id,name,description,price,stock,emoji,created_at) VALUES(?,?,?,?,?,?,?)",(cat,name,desc,price,stock,emoji,now()))
            # 可重复的历史销售样本，供图表、画像、预测和推荐演示
            customers=[1,2,3]
            for day in range(-13,1):
                for j in range(1+(day%3)):
                    uid=customers[(day+j)%3]; oid=c.execute("INSERT INTO orders(user_id,total,status,created_at) VALUES(?,?,?,?)",(uid,0,"已付款",today(day)+f" {10+j}:20:00")).lastrowid
                    picks=[((abs(day)*2+j)%12)+1, ((abs(day)+j+3)%12)+1]
                    total=0
                    for pid in dict.fromkeys(picks):
                        p=c.execute("SELECT * FROM products WHERE id=?",(pid,)).fetchone(); qty=1+(pid%2); total+=p['price']*qty
                        c.execute("INSERT INTO order_items(order_id,product_id,product_name,category_id,unit_price,qty) VALUES(?,?,?,?,?,?)",(oid,pid,p['name'],p['category_id'],p['price'],qty))
                    c.execute("UPDATE orders SET total=? WHERE id=?",(total,oid))
            c.commit()

def rows(c, sql, args=()): return [dict(x) for x in c.execute(sql,args).fetchall()]

class App(BaseHTTPRequestHandler):
    server_version="SmartShop/1.0"
    def log_message(self, fmt, *args): print(f"[{now()}] {self.address_string()} {fmt%args}")
    def ip(self): return self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
    def body(self):
        try: return json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))) or b"{}")
        except Exception: return {}
    def send_json(self, obj, status=200):
        data=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(data))); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(data)
    def send_file(self,path):
        if not path.exists(): return self.send_error(404)
        data=path.read_bytes(); self.send_response(200); self.send_header("Content-Type",mimetypes.guess_type(path.name)[0] or "application/octet-stream"); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data)
    def user(self):
        token=self.headers.get("Authorization","").removeprefix("Bearer ").strip()
        if not token:return None
        with db() as c:
            r=c.execute("SELECT u.id,u.username,u.email,u.role,u.region FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires_at>? AND u.active=1",(token,int(time.time()))).fetchone()
            return dict(r) if r else None
    def require(self,*roles):
        u=self.user()
        if not u: self.send_json({"error":"请先登录"},401); return None
        if roles and u["role"] not in roles: self.send_json({"error":"无权执行此操作"},403); return None
        return u
    def audit(self,c,u,content): c.execute("INSERT INTO operation_logs(user_id,username,content,ip,created_at) VALUES(?,?,?,?,?)",(u['id'],u['username'],content,self.ip(),now()))
    def route(self): return urlparse(self.path)
    def do_GET(self):
        p=self.route(); path=p.path; q=parse_qs(p.query)
        if not path.startswith("/api/"):
            rel="index.html" if path=="/" else path.lstrip("/"); target=(STATIC/rel).resolve()
            return self.send_file(target if str(target).startswith(str(STATIC.resolve())) and target.exists() else STATIC/"index.html")
        try:
            if path=="/api/health": return self.send_json({"status":"ok","service":"智购洞察","time":now()})
            if path=="/api/me": return self.send_json({"user":self.user()})
            if path=="/api/categories":
                with db() as c:return self.send_json(rows(c,"SELECT * FROM categories ORDER BY id"))
            if path=="/api/products":
                kw=q.get("q",[""])[0]; cat=q.get("category",[""])[0]
                sql="SELECT p.*,c.name category FROM products p JOIN categories c ON c.id=p.category_id WHERE p.status='上架'"; args=[]
                if kw: sql+=" AND (p.name LIKE ? OR p.description LIKE ?)"; args += [f"%{kw}%",f"%{kw}%"]
                if cat: sql+=" AND p.category_id=?"; args.append(cat)
                with db() as c:return self.send_json(rows(c,sql+" ORDER BY p.id",args))
            if path.startswith("/api/products/"):
                pid=int(path.rsplit("/",1)[1]); u=self.user()
                with db() as c:
                    pdt=c.execute("SELECT p.*,c.name category FROM products p JOIN categories c ON c.id=p.category_id WHERE p.id=?",(pid,)).fetchone()
                    if not pdt:return self.send_json({"error":"商品不存在"},404)
                    c.execute("INSERT INTO browse_logs(user_id,product_id,category_id,seconds,ip,created_at) VALUES(?,?,?,?,?,?)",(u['id'] if u else None,pid,pdt['category_id'],int(q.get('seconds',[8])[0]),self.ip(),now()))
                    related=rows(c,"SELECT p.*,c.name category FROM products p JOIN categories c ON c.id=p.category_id WHERE p.id IN (SELECT oi2.product_id FROM order_items oi1 JOIN order_items oi2 ON oi1.order_id=oi2.order_id AND oi1.product_id<>oi2.product_id WHERE oi1.product_id=? GROUP BY oi2.product_id ORDER BY COUNT(*) DESC) LIMIT 4",(pid,))
                    if not related: related=rows(c,"SELECT p.*,c.name category FROM products p JOIN categories c ON c.id=p.category_id WHERE p.category_id=? AND p.id<>? ORDER BY p.stock DESC LIMIT 4",(pdt['category_id'],pid))
                    return self.send_json({"product":dict(pdt),"related":related})
            if path=="/api/cart":
                u=self.require("customer");
                if not u:return
                with db() as c:return self.send_json(rows(c,"SELECT ci.product_id,ci.qty,p.name,p.price,p.stock,p.emoji,(ci.qty*p.price) subtotal FROM cart_items ci JOIN products p ON p.id=ci.product_id WHERE ci.user_id=?",(u['id'],)))
            if path=="/api/recommendations":
                u=self.user()
                with db() as c:
                    if not u: rec=rows(c,"SELECT p.*,c.name category FROM products p JOIN categories c ON c.id=p.category_id LEFT JOIN order_items oi ON oi.product_id=p.id GROUP BY p.id ORDER BY COALESCE(SUM(oi.qty),0) DESC LIMIT 4")
                    else:
                        # 用户协同过滤：Jaccard 相似购买者的未购商品加权得分
                        mine={x[0] for x in c.execute("SELECT DISTINCT oi.product_id FROM orders o JOIN order_items oi ON oi.order_id=o.id WHERE o.user_id=?",(u['id'],))}
                        scores={}
                        for other in c.execute("SELECT id FROM users WHERE role='customer' AND id<>?",(u['id'],)):
                            theirs={x[0] for x in c.execute("SELECT DISTINCT oi.product_id FROM orders o JOIN order_items oi ON oi.order_id=o.id WHERE o.user_id=?",(other[0],))}
                            sim=len(mine&theirs)/len(mine|theirs) if mine|theirs else 0
                            for pid in theirs-mine:scores[pid]=scores.get(pid,0)+sim
                        ids=[x[0] for x in sorted(scores.items(),key=lambda x:x[1],reverse=True)[:4]]
                        rec=rows(c,"SELECT p.*,c.name category FROM products p JOIN categories c ON c.id=p.category_id WHERE p.id IN (%s)"%(','.join('?'*len(ids))),ids) if ids else []
                        if not rec: rec=rows(c,"SELECT p.*,c.name category FROM products p JOIN categories c ON c.id=p.category_id LEFT JOIN order_items oi ON oi.product_id=p.id GROUP BY p.id ORDER BY COALESCE(SUM(oi.qty),0) DESC LIMIT 4")
                    return self.send_json(rec)
            if path=="/api/orders":
                u=self.require();
                if not u:return
                with db() as c:
                    sql="SELECT o.*,u.username FROM orders o JOIN users u ON u.id=o.user_id"; args=()
                    if u['role']=='customer':sql+=" WHERE o.user_id=?";args=(u['id'],)
                    return self.send_json(rows(c,sql+" ORDER BY o.id DESC LIMIT 100",args))
            if path=="/api/analytics":
                u=self.require("sales","admin");
                if not u:return
                with db() as c:return self.send_json(analytics(c))
            if path=="/api/logs":
                u=self.require("sales","admin");
                if not u:return
                with db() as c:return self.send_json({"login":rows(c,"SELECT * FROM login_logs ORDER BY id DESC LIMIT 40"),"browse":rows(c,"SELECT b.*,p.name product FROM browse_logs b JOIN products p ON p.id=b.product_id ORDER BY b.id DESC LIMIT 40"),"operations":rows(c,"SELECT * FROM operation_logs ORDER BY id DESC LIMIT 40")})
            if path=="/api/users/sales":
                u=self.require("admin");
                if not u:return
                with db() as c:return self.send_json(rows(c,"SELECT id,username,email,active,created_at FROM users WHERE role='sales' ORDER BY id"))
            if path=="/api/export.csv":
                u=self.require("sales","admin");
                if not u:return
                with db() as c:data=rows(c,"SELECT o.id,u.username,o.total,o.status,o.created_at FROM orders o JOIN users u ON u.id=o.user_id ORDER BY o.id")
                out=io.StringIO(); w=csv.DictWriter(out,fieldnames=["id","username","total","status","created_at"]);w.writeheader();w.writerows(data);raw=('\ufeff'+out.getvalue()).encode('utf-8');self.send_response(200);self.send_header('Content-Type','text/csv; charset=utf-8');self.send_header('Content-Disposition','attachment; filename=orders.csv');self.send_header('Content-Length',str(len(raw)));self.end_headers();return self.wfile.write(raw)
            return self.send_json({"error":"接口不存在"},404)
        except Exception as e: print("ERROR",repr(e)); return self.send_json({"error":"服务器处理失败","detail":str(e)},500)
    def do_POST(self):
        path=self.route().path; b=self.body()
        try:
            if path=="/api/register":
                if not all(b.get(x) for x in ("username","email","password")) or len(b.get("password",""))<6:return self.send_json({"error":"请完整填写，密码至少 6 位"},400)
                with db() as c:
                    try:c.execute("INSERT INTO users(username,email,password,role,region,created_at) VALUES(?,?,?,?,?,?)",(b['username'].strip(),b['email'].strip(),password_hash(b['password']),"customer",b.get('region','广东'),now()));c.commit()
                    except sqlite3.IntegrityError:return self.send_json({"error":"用户名已存在"},409)
                return self.login(b['username'],b['password'])
            if path=="/api/login": return self.login(b.get('username',''),b.get('password',''))
            if path=="/api/logout":
                token=self.headers.get("Authorization","").removeprefix("Bearer ").strip()
                with db() as c:c.execute("DELETE FROM sessions WHERE token=?",(token,));c.commit()
                return self.send_json({"ok":True})
            if path=="/api/cart":
                u=self.require("customer");
                if not u:return
                pid=int(b.get('product_id',0));qty=max(1,int(b.get('qty',1)))
                with db() as c:
                    p=c.execute("SELECT stock FROM products WHERE id=? AND status='上架'",(pid,)).fetchone()
                    if not p:return self.send_json({"error":"商品不存在"},404)
                    qty=min(qty,p['stock']);c.execute("INSERT INTO cart_items VALUES(?,?,?) ON CONFLICT(user_id,product_id) DO UPDATE SET qty=min(qty+excluded.qty,?)",(u['id'],pid,qty,p['stock']));c.commit()
                return self.send_json({"ok":True})
            if path=="/api/checkout":
                u=self.require("customer");
                if not u:return
                with db() as c:
                    items=c.execute("SELECT ci.*,p.name,p.price,p.stock,p.category_id FROM cart_items ci JOIN products p ON p.id=ci.product_id WHERE ci.user_id=?",(u['id'],)).fetchall()
                    if not items:return self.send_json({"error":"购物车为空"},400)
                    if any(x['qty']>x['stock'] for x in items):return self.send_json({"error":"部分商品库存不足"},409)
                    total=sum(x['qty']*x['price'] for x in items);oid=c.execute("INSERT INTO orders(user_id,total,status,created_at) VALUES(?,?,?,?)",(u['id'],total,"已付款",now())).lastrowid
                    for x in items:
                        c.execute("INSERT INTO order_items(order_id,product_id,product_name,category_id,unit_price,qty) VALUES(?,?,?,?,?,?)",(oid,x['product_id'],x['name'],x['category_id'],x['price'],x['qty']));c.execute("UPDATE products SET stock=stock-? WHERE id=?",(x['qty'],x['product_id']))
                    c.execute("DELETE FROM cart_items WHERE user_id=?",(u['id'],));c.execute("INSERT INTO email_logs(user_id,subject,content,created_at) VALUES(?,?,?,?)",(u['id'],f"订单 #{oid} 支付成功",f"订单金额 ¥{total:.2f}，感谢购买。",now()));c.commit()
                return self.send_json({"ok":True,"order_id":oid,"total":total})
            if path=="/api/products":
                u=self.require("sales","admin");
                if not u:return
                with db() as c:
                    cur=c.execute("INSERT INTO products(category_id,name,description,price,stock,emoji,created_at) VALUES(?,?,?,?,?,?,?)",(int(b['category_id']),b['name'],b.get('description',''),float(b['price']),int(b['stock']),b.get('emoji','📦'),now()));self.audit(c,u,f"新增商品 #{cur.lastrowid} {b['name']}");c.commit()
                return self.send_json({"ok":True,"id":cur.lastrowid},201)
            if path=="/api/categories":
                u=self.require("sales","admin");
                if not u:return
                with db() as c:
                    try:cur=c.execute("INSERT INTO categories(name) VALUES(?)",(b['name'],));self.audit(c,u,f"新增分类 {b['name']}");c.commit()
                    except sqlite3.IntegrityError:return self.send_json({"error":"分类已存在"},409)
                return self.send_json({"ok":True,"id":cur.lastrowid},201)
            if path=="/api/users/sales":
                u=self.require("admin");
                if not u:return
                with db() as c:
                    try:cur=c.execute("INSERT INTO users(username,email,password,role,region,created_at) VALUES(?,?,?,?,?,?)",(b['username'],b['email'],password_hash(b.get('password','sales123')),'sales',b.get('region','广东'),now()));self.audit(c,u,f"新增销售账号 {b['username']}");c.commit()
                    except sqlite3.IntegrityError:return self.send_json({"error":"用户名已存在"},409)
                return self.send_json({"ok":True,"id":cur.lastrowid},201)
            return self.send_json({"error":"接口不存在"},404)
        except Exception as e: print("ERROR",repr(e));return self.send_json({"error":"请求处理失败","detail":str(e)},500)
    def login(self,username,password):
        with db() as c:
            r=c.execute("SELECT * FROM users WHERE username=?",(username,)).fetchone();ok=bool(r and r['active'] and password_ok(password,r['password']));c.execute("INSERT INTO login_logs(username,role,ip,success,created_at) VALUES(?,?,?,?,?)",(username,r['role'] if r else '',self.ip(),1 if ok else 0,now()))
            if not ok:c.commit();return self.send_json({"error":"用户名或密码错误"},401)
            token=secrets.token_urlsafe(32);c.execute("INSERT INTO sessions VALUES(?,?,?)",(token,r['id'],int(time.time())+86400));c.commit();return self.send_json({"token":token,"user":{"id":r['id'],"username":r['username'],"email":r['email'],"role":r['role'],"region":r['region']}})
    def do_PATCH(self):
        path=self.route().path;b=self.body()
        try:
            if path.startswith('/api/cart/'):
                u=self.require('customer');
                if not u:return
                pid=int(path.rsplit('/',1)[1]);qty=max(0,int(b.get('qty',0)))
                with db() as c:
                    if qty:c.execute("UPDATE cart_items SET qty=? WHERE user_id=? AND product_id=?",(qty,u['id'],pid))
                    else:c.execute("DELETE FROM cart_items WHERE user_id=? AND product_id=?",(u['id'],pid))
                    c.commit()
                return self.send_json({'ok':True})
            if path.startswith('/api/products/'):
                u=self.require('sales','admin');
                if not u:return
                pid=int(path.rsplit('/',1)[1]); allowed={'name','description','price','stock','category_id','status','emoji'}; fields=[k for k in b if k in allowed]
                if not fields:return self.send_json({'error':'没有可更新字段'},400)
                with db() as c:c.execute("UPDATE products SET "+','.join(f"{k}=?" for k in fields)+" WHERE id=?",[b[k] for k in fields]+[pid]);self.audit(c,u,f"修改商品 #{pid}: {','.join(fields)}");c.commit()
                return self.send_json({'ok':True})
            if path.startswith('/api/users/sales/'):
                u=self.require('admin');
                if not u:return
                uid=int(path.split('/')[4])
                with db() as c:
                    if 'password' in b:c.execute("UPDATE users SET password=? WHERE id=? AND role='sales'",(password_hash(b['password']),uid));action='重置密码'
                    else:c.execute("UPDATE users SET active=? WHERE id=? AND role='sales'",(1 if b.get('active') else 0,uid));action='更新状态'
                    self.audit(c,u,f"{action}：销售账号 #{uid}");c.commit()
                return self.send_json({'ok':True})
            return self.send_json({'error':'接口不存在'},404)
        except Exception as e:return self.send_json({'error':'请求处理失败','detail':str(e)},500)

def analytics(c):
    trend=rows(c,"SELECT substr(created_at,1,10) label,ROUND(SUM(total),2) sales,COUNT(*) orders FROM orders WHERE status='已付款' GROUP BY label ORDER BY label DESC LIMIT 14")[::-1]
    ys=[x['sales'] for x in trend]; n=len(ys); forecast=[]
    if n:
        xs=list(range(n));xm=sum(xs)/n;ym=sum(ys)/n;den=sum((x-xm)**2 for x in xs) or 1;slope=sum((x-xm)*(y-ym) for x,y in zip(xs,ys))/den;inter=ym-slope*xm
        for i in range(1,8):forecast.append({'label':today(i),'sales':round(max(0,inter+slope*(n-1+i)),2)})
    ranking=rows(c,"SELECT p.name,p.emoji,SUM(oi.qty) qty,ROUND(SUM(oi.qty*oi.unit_price),2) revenue FROM order_items oi JOIN products p ON p.id=oi.product_id GROUP BY p.id ORDER BY qty DESC LIMIT 8")
    profiles=rows(c,"SELECT u.username,u.region,ROUND(COALESCE(SUM(o.total),0),2) spending,CASE WHEN COALESCE(SUM(o.total),0)>=6000 THEN '高' WHEN COALESCE(SUM(o.total),0)>=2500 THEN '中' ELSE '低' END purchasing_power,(SELECT c2.name FROM order_items i2 JOIN orders o2 ON o2.id=i2.order_id JOIN categories c2 ON c2.id=i2.category_id WHERE o2.user_id=u.id GROUP BY c2.id ORDER BY SUM(i2.qty) DESC LIMIT 1) preference FROM users u LEFT JOIN orders o ON o.user_id=u.id WHERE u.role='customer' GROUP BY u.id ORDER BY spending DESC")
    low=rows(c,"SELECT id,name,stock FROM products WHERE stock<=5 ORDER BY stock")
    avg=statistics.mean(ys[-8:-1]) if len(ys)>1 else (ys[0] if ys else 0);cur=ys[-1] if ys else 0;anomalies=[]
    if cur>avg*1.8 and avg>0:anomalies.append({'level':'高','message':f'今日销售额较近 7 日均值高 {((cur/avg)-1)*100:.0f}%'})
    if cur<avg*.3 and avg>0:anomalies.append({'level':'中','message':'今日销售额显著低于近期均值'})
    anomalies += [{'level':'中','message':f"{x['name']} 库存仅 {x['stock']} 件"} for x in low]
    totals={'revenue':round(sum(ys),2),'orders':c.execute("SELECT COUNT(*) FROM orders").fetchone()[0],'customers':c.execute("SELECT COUNT(*) FROM users WHERE role='customer'").fetchone()[0],'views':c.execute("SELECT COUNT(*) FROM browse_logs").fetchone()[0]}
    return {'totals':totals,'trend':trend,'forecast':forecast,'ranking':ranking,'profiles':profiles,'anomalies':anomalies}

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--host',default='0.0.0.0');ap.add_argument('--port',type=int,default=18080);args=ap.parse_args();init_db();print(f"SmartShop listening on http://{args.host}:{args.port}");ThreadingHTTPServer((args.host,args.port),App).serve_forever()
