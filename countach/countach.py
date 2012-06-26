import re
import os
import sys
import glob
import copy
import shutil
import optparse
import logging
import time
import template
import markmin2html
import contenttype
import shelve
from wsgiref.simple_server import make_server
from BeautifulSoup import BeautifulSoup as Soup

try:
    import markdown
except ImportError:
    logging.error('failure to import markdown, need pip install markdown')
    sys.exit(1)

try:
    from docutils.core import publish_string, publish_parts
except ImportError:
    logging.error('failure to import reST, need pip install docutils')
    sys.exit(1)

try:
    import tornado.web
    import tornado.wsgi
    import tornado.httpserver
    import tornado.ioloop
    have_tornado = True
except ImportError:
    have_tornado = False

"""
domain:
  language:
    ~layout.html
    index.md  -> index.html (extends layout)
    about.md  -> about.html (extnds layout+about_layout+
                                            about#footer+about#posts)
    about~layout.html -> ..
    
    <optional>
    about#footer.html -> ..
    about#posts/
       post1.md       -> about/post1.html (exends layout+about_layout+
                                           about#footer)
       post2.md       -> ..
    </optional>
    about/
       post3.md       -> about/post3.html (exends layout+about_layout+
                                           about#footer) but not embedded 
                                           about.html
    more/
       post4.md       -> more/post4.html (exends layout.html only)
       post5.md       -> more/post5.html (extends layout.html only)

"""


#####

RE_LAYOUT = re.compile('layout.html')
RE_EXTENDED_LAYOUT = re.compile('(?P<page>.+?)_layout\.html')
FORMATTERS = {
    'html': lambda text: text,
    'mm': lambda text: markmin2html(text),
    'rst': lambda text: \
        publish_parts(text, writer_name="html")['body'],
    'rst2': lambda text: \
        publish_string(text, writer=HisWriter(),
                       settings=None, settings_overrides={}),
    'md': lambda text: markdown.markdown(text),
}


def walk(path,ignore=None):
    """ loop over all files in a tree, recursively """
    paths = [path]
    while paths:
        path = paths.pop()
        for f in os.listdir(path):
            discovery = os.path.join(path,f)
            if ignore and ignore(discovery):
                continue
            if os.path.isdir(discovery):
                paths.append(discovery)
            else:
                yield discovery

def parent(layouts,path):
    """ given a dict of layouts, finds the closest matching the path """
    fragments = path.split('/')
    for i in range(len(fragments),0,-1):
        sub = os.path.join(*fragments[:i])
        if sub in layouts:
            return layouts[sub]
    return None

def layout_merge(layout,newlayout):
    """ uses newlayout to extend layout (which can be None)"""
    if not layout:
        return newlayout
    layout = Soup(str(layout))
    head = newlayout.find('head')
    if head:
        layout.find('head').replaceWith(head)
    else:            
        title = newlayout.find('title')
        if title:
            layout.find('title').replaceWith(title)
        for meta in newlayout.findAll('meta'):
            layout.find('head').append(meta)
        for style in newlayout.findAll('style'):
            layout.find('head').append(style)
        for script in newlayout.findAll('script'):
            layout.find('head').append(script)    
    body = newlayout.find('body') 
    if body:
        layout.find('body').replaceWith(body)
    else:
        for div in newlayout.findAll('div'):
            try:
                olddiv = layout.find('div',id=div['id'])            
                if olddiv: olddiv.replaceWith(div)
            except: pass
    return layout

class Codes(object):
    def __init__(self):
        self.items = []
        self.regex = re.compile('\{\{.*?\}\}',re.M)
    def _in(self,match):
        self.items.insert(0,match.group())
        return '{{#}}'
    def _out(self,match):
        return self.items.pop()
    def replace_in(self,text):
        return self.regex.sub(self._in,text)
    def replace_out(self,text):
        return self.regex.sub(self._out,text)

def process(input, output, prefix):
    """
    recursively walks through the input folder, and processes all files,
    stores output in output folder
    - extends layouts as walking down three
    - supports html, rst, markdown, and markmin for file content
    - compresses js (work in progress)
    - processes less - > css (work in progress)
    - supports web2py template language for any file type
    """
    env = {'prefix':prefix,'now':time.ctime()}
    processed = set()
    print os.path.join(input,'_filelist.shelve')
    current = shelve.open(os.path.join(input,'_filelist.shelve'))
    layouts = {}
    for filename in walk(input):
        path,name = os.path.split(filename)
        base,extension = name.rsplit('.',1) if '.' in name else (name,'html')
        layout = parent(layouts,filename)
        dest = output+path[len(input):]
        try: os.makedirs(dest)
        except OSError: pass
        if '#' in name or name.endswith('~') or RE_EXTENDED_LAYOUT.match(name):
            continue
        elif name == '_layout.html':
            print '<-',filename
            newlayout = Soup(open(filename,'rb').read())
            layouts[path] = layout_merge(layout,newlayout)        
        elif not name.startswith('_') and not '#' in name and \
                not name.endswith('~') and extension in FORMATTERS:
            print '<-',filename
            text = open(filename).read()     
            layoutname = os.path.join(path,base+'_layout.html')
            layout = parent(layouts,path)
            if os.path.exists(layoutname):
                print '    <-',layoutname
                newlayout =Soup(open(layoutname).read())                
                layout = layout_merge(layout,newlayout)
            for diff in glob.glob(os.path.join(path,base+'#*.*')):
                if diff.endswith('~'): continue
                print '    <-',diff
                id = diff.split('#')[1].split('.')[0]                
                component_html = FORMATTERS[extension](open(diff).read())
                component = Soup('<div id="%s">%s</div>' % (id, component_html))
                layout = layout_merge(layout,component)
            codes = Codes()
            output_filename = os.path.join(dest,base+'.html')
            env['filename'] = output_filename[:len(output)]
            text = codes.replace_in(text)
            component_html = FORMATTERS[extension](text)
            component_html = codes.replace_out(component_html)
            component = Soup('<div id="page-content">%s</div>' % component_html)            
            layout = layout_merge(layout,component)
            layouts[filename[:-1-len(extension)]] = layout

            html = template.render(str(layout),context=copy.copy(env))

            print '->',output_filename
            key = output_filename[len(output):]
            current[key] = 1
            processed.add(key)
            open(output_filename,'wb').write(html)
        elif name.startswith('_'):
            continue
        elif '#eval.' in filename:
            output_filename = os.path.join(dest,name.replace('#eval.','.'))
            env['filename']=output_filename
            data = open(filename,'rb').read()
            data = template.render(data,context=copy.copy(env))
            print '->',output_filename,'(eval)'
            open(output_filename,'wb').write(data)
        else:
            output_filename = os.path.join(dest,name)
            print '->',output_filename,'(copy)'
            shutil.copyfile(filename,output_filename)            
    missing = [key for key in current if not key in processed]
    for key in missing: del current[key]
    update_missing(missing,output)

def update_missing(missing,output):
    """
    updates a list of missing files which may need to redirect somewhere
    """
    filename = os.path.join(output,'routes.in')
    if os.path.exists(filename):
        routes_in = [[x.strip() for x in r.split('>')] for r in open(filename)]
    else:
        routes_in = []
    keys = set(r[0] for r in routes_in)
    for key in missing:
        routes_in.append((key,'/404.html # new'))
    routes_in.sort()
    open(filename,'wb').write('\n'.join('%s > %s' % r for r in routes_in))

def getlanguage(languages,default):
    codes = [lang.strip().split(";")[0] for lang in languages.split(',')]
    codes.append(default)
    return codes

def static_app_factory(basepath):
    routes_fn = os.path.join(basepath,'routes.in')
    routes_re = []
    for r in open(routes_fn):
        if not r.startswith('#') and not r.strip():
            regex, sub = r.split('>')[:2]
            routes_re.append((re.compile(regex),sub))
    def main_app(environ,start_response):
        host = environ.get('HTTP_HOST','127.0.0.1')
        path = environ.get('PATH_INFO','/')[1:] or 'index.html'
        if not '.' in os.path.split(path)[-1]: path = path+'.html'
        accept_languages = environ.get('HTTP_ACCEPT_LANGUAGE','en')
        domain = host.split(':')[0]
        if not os.path.exists(os.path.join(basepath,domain)):
            domain = '127.0.0.1'
        for lang in getlanguage(accept_languages,'en'):
            if os.path.exists(os.path.join(basepath,domain,lang)):
                break
        relpath = '%s/%s/%s' % (domain, lang, path)
        filepath = os.path.join(basepath,relpath)
        # if no extension add .html
        try:
            status = '200 OK' # assume!
            data = open(filepath,'rb').read()
            content_type = contenttype.contenttype(filepath)            
            response_headers = [('Content-type',content_type)]
        except IOError:
            for regex,sub in routes_re:
                if regex.match(relpath):
                    newpath = regex.sub(sub,path)
                    status = '301 REDIRECT'
                    response_headers = [('Location',newpath)]
                    body = ''
                    break
            else:
                status = '404 NOT FOUND'
                response_headers = [('Content-type','text/html')]
                relpath = '%s/%s/404.html' % (domain, lang)
                if os.path.exists(relpath):
                    filepath = os.path.join(basepath,relpath)
                    data = open(filepath,'rb').read()
                else:
                    data = 'Error: File Not Found!'
        start_response(status, response_headers)
        return [data]
    return main_app
    
def main():
    usage = "countach/countach.py input output"  
    version = "0.1"
    parser = optparse.OptionParser(usage, None, optparse.Option, version)
    parser.add_option("-c", "--clear", dest="clear", default=None,
                      action='store_true', help="clear")
    parser.add_option("-i", "--input", dest="input", default='input',
                      help="input folder")
    parser.add_option("-o", "--output", dest="output", default='output',
                      help="output folder")
    parser.add_option("-s", "--serve", dest="serve", default=None,
                      action='store_true', help="start web server from folder")
    parser.add_option("-a", "--address", dest="address", default='127.0.0.1:8000',
                      help="server address")
    parser.add_option("-p", "--prefix", dest="prefix", default='',
                      help="prefix to be added to the path of links")
    (options, args) = parser.parse_args()
    if options.serve:
        address,port = options.address.split(':')
        port = int(port)
        wsgiapp = static_app_factory(basepath=options.output)
        
        # Respond to requests until process is killed
        try:
            if have_tornado:
                print "Serving from %s using tornado ..." % options.address
                container = tornado.wsgi.WSGIContainer(wsgiapp)
                server = tornado.httpserver.HTTPServer(container)
                server.listen(address=address, port=port)
                tornado.ioloop.IOLoop.instance().start()
            else:    
                print "Serving from %s using wsgiref ..." % options.address
                make_server(address,int(port),wsgiapp).serve_forever()        
        except KeyboardInterrupt:
            print "Server shut down."
        sys.exit(0)
    if options.clear:
        shutil.rmtree(options.output)
    process(options.input,options.output,options.prefix)
    
if __name__=='__main__': main()
