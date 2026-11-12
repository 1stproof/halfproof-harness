"""Single bounded source download child; parent owns its total wall and process."""
import base64
import sys
import urllib.parse
import urllib.request

MAX_BYTES=4*1024*1024


def allowed(url):
    p=urllib.parse.urlsplit(url)
    if p.scheme!='https' or p.netloc not in ('arxiv.org','export.arxiv.org') or not p.path.startswith(('/e-print/','/html/')) or p.query or p.fragment:
        raise ValueError('unapproved source URL')


def identity(url):
    p=urllib.parse.urlsplit(url)
    return ('https', 'arxiv.org', p.path, p.query, p.fragment)


class Redirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, url=None):
        super().__init__(); self.source_identity=identity(url) if url is not None else None
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        allowed(newurl)
        if self.source_identity is None:self.source_identity=identity(req.full_url)
        if identity(newurl) != self.source_identity:
            raise ValueError('source redirect changed pinned identity')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def main(url, cap=MAX_BYTES):
    if not 1 <= cap <= 32*1024*1024:raise ValueError("source cap bound")
    allowed(url)
    with urllib.request.build_opener(Redirect(url)).open(urllib.request.Request(url,headers={'User-Agent':'harness-v3-source-verifier/1.0'}),timeout=10) as stream:
        data=stream.read(cap+1)
    if len(data)>cap:raise ValueError('source byte bound exceeded')
    sys.stdout.write(base64.b64encode(data).decode('ascii'))


if __name__=='__main__':main(sys.argv[1], int(sys.argv[2]) if len(sys.argv)>2 else MAX_BYTES)
