''' E-book management software'''
__license__   = 'GPL v3'
__copyright__ = '2008, Kovid Goyal <kovid at kovidgoyal.net>'
__version__   = '0.4.82'
__docformat__ = "epytext"
__author__    = "Kovid Goyal <kovid at kovidgoyal.net>"
__appname__   = 'calibre'

import sys, os, logging, mechanize, locale, copy, cStringIO, re, subprocess, \
       textwrap, atexit, cPickle, codecs, time
from gettext import GNUTranslations
from htmlentitydefs import name2codepoint
from math import floor
from optparse import OptionParser as _OptionParser
from optparse import IndentedHelpFormatter
from logging import Formatter

from PyQt4.QtCore import QSettings, QVariant, QUrl, QByteArray, QString
from PyQt4.QtGui import QDesktopServices

from calibre.translations.msgfmt import make
from calibre.ebooks.chardet import detect
from calibre.utils.terminfo import TerminalController

terminal_controller = TerminalController(sys.stdout)
iswindows = 'win32' in sys.platform.lower() or 'win64' in sys.platform.lower()
isosx     = 'darwin' in sys.platform.lower()
islinux   = not(iswindows or isosx)
isfrozen  = hasattr(sys, 'frozen') 

try:
    locale.setlocale(locale.LC_ALL, '')
except:
    dl = locale.getdefaultlocale()
    try:
        if dl:
            locale.setlocale(dl[0])
    except:
        pass

try:
    preferred_encoding = locale.getpreferredencoding()
    codecs.lookup(preferred_encoding)
except:
    preferred_encoding = 'utf-8'

if getattr(sys, 'frozen', False):
    if iswindows:
        plugin_path = os.path.join(os.path.dirname(sys.executable), 'plugins')
    elif isosx:
        plugin_path = os.path.join(getattr(sys, 'frameworks_dir'), 'plugins')
    elif islinux:
        plugin_path = os.path.join(getattr(sys, 'frozen_path'), 'plugins')
    sys.path.insert(0, plugin_path)
else:
    import pkg_resources
    plugins = getattr(pkg_resources, 'resource_filename')(__appname__, 'plugins')
    sys.path.insert(0, plugins)
    
if iswindows and getattr(sys, 'frozen', False):
    sys.path.insert(1, os.path.dirname(sys.executable))


plugins = {}
for plugin in ['pictureflow', 'lzx', 'msdes'] + \
            (['winutil'] if iswindows else []) + \
            (['usbobserver'] if isosx else []):
    try:
        p, err = __import__(plugin), ''
    except Exception, err:
        p = None
        err = str(err)
    plugins[plugin] = (p, err)

if iswindows:
    winutil, winutilerror = plugins['winutil']
    if not winutil:
        raise RuntimeError('Failed to load the winutil plugin: %s'%winutilerror)
    sys.argv[1:] = winutil.argv()[1:]
    win32event = __import__('win32event')
    winerror   = __import__('winerror')
    win32api   = __import__('win32api')
else:
    import fcntl

_abspath = os.path.abspath
def my_abspath(path, encoding=sys.getfilesystemencoding()):
    '''
    Work around for buggy os.path.abspath. This function accepts either byte strings,
    in which it calls os.path.abspath, or unicode string, in which case it first converts
    to byte strings using `encoding`, calls abspath and then decodes back to unicode.
    '''
    to_unicode = False
    if isinstance(path, unicode):
        path = path.encode(encoding)
        to_unicode = True
    res = _abspath(path)
    if to_unicode:
        res = res.decode(encoding)
    return res

os.path.abspath = my_abspath
_join = os.path.join
def my_join(a, *p):
    encoding=sys.getfilesystemencoding()
    p = [a] + list(p)
    _unicode = False
    for i in p:
        if isinstance(i, unicode):
            _unicode = True
            break
    p = [i.encode(encoding) if isinstance(i, unicode) else i for i in p]
    
    res = _join(*p)
    if _unicode:
        res = res.decode(encoding)
    return res

os.path.join = my_join

def unicode_path(path, abs=False):
    if not isinstance(path, unicode):
        path = path.decode(sys.getfilesystemencoding())
    if abs:
        path = os.path.abspath(path)
    return path

def osx_version():
    if isosx:
        import platform
        src = platform.mac_ver()[0]
        m = re.match(r'(\d+)\.(\d+)\.(\d+)', src)
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3))
        

# Default translation is NOOP
import __builtin__
__builtin__.__dict__['_'] = lambda s: s

class CommandLineError(Exception):
    pass

class ColoredFormatter(Formatter):
    
    def format(self, record):
        ln = record.__dict__['levelname']
        col = ''
        if ln == 'CRITICAL':
            col = terminal_controller.YELLOW
        elif ln == 'ERROR':
            col = terminal_controller.RED
        elif ln in ['WARN', 'WARNING']:
            col = terminal_controller.BLUE
        elif ln == 'INFO':
            col = terminal_controller.GREEN
        elif ln == 'DEBUG':
            col = terminal_controller.CYAN
        record.__dict__['levelname'] = col + record.__dict__['levelname'] + terminal_controller.NORMAL
        return Formatter.format(self, record)
         

def setup_cli_handlers(logger, level):
    if os.environ.get('CALIBRE_WORKER', None) is not None and logger.handlers:
        return
    logger.setLevel(level)
    if level == logging.WARNING:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
        handler.setLevel(logging.WARNING)
    elif level == logging.INFO:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter())
        handler.setLevel(logging.INFO)
    elif level == logging.DEBUG:
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter('[%(levelname)s] %(filename)s:%(lineno)s: %(message)s'))
    
    logger.addHandler(handler)

class CustomHelpFormatter(IndentedHelpFormatter):
    
    def format_usage(self, usage):
        return _("%sUsage%s: %s\n") % (terminal_controller.BLUE, terminal_controller.NORMAL, usage)
    
    def format_heading(self, heading):
        return "%*s%s%s%s:\n" % (self.current_indent, terminal_controller.BLUE, 
                                 "", heading, terminal_controller.NORMAL)
        
    def format_option(self, option):
        result = []
        opts = self.option_strings[option]
        opt_width = self.help_position - self.current_indent - 2
        if len(opts) > opt_width:
            opts = "%*s%s\n" % (self.current_indent, "", 
                                    terminal_controller.GREEN+opts+terminal_controller.NORMAL)
            indent_first = self.help_position
        else:                       # start help on same line as opts
            opts = "%*s%-*s  " % (self.current_indent, "", opt_width + len(terminal_controller.GREEN + terminal_controller.NORMAL), 
                                  terminal_controller.GREEN + opts + terminal_controller.NORMAL)
            indent_first = 0
        result.append(opts)
        if option.help:
            help_text = self.expand_default(option).split('\n')
            help_lines = []
            
            for line in help_text:
                help_lines.extend(textwrap.wrap(line, self.help_width))
            result.append("%*s%s\n" % (indent_first, "", help_lines[0]))
            result.extend(["%*s%s\n" % (self.help_position, "", line)
                           for line in help_lines[1:]])
        elif opts[-1] != "\n":
            result.append("\n")
        return "".join(result)+'\n'

class OptionParser(_OptionParser):
    
    def __init__(self,
                 usage='%prog [options] filename',
                 version='%%prog (%s %s)'%(__appname__, __version__),
                 epilog=_('Created by ')+terminal_controller.RED+__author__+terminal_controller.NORMAL,
                 gui_mode=False,
                 conflict_handler='resolve',
                 **kwds):
        usage += '''\n\nWhenever you pass arguments to %prog that have spaces in them, '''\
                 '''enclose the arguments in quotation marks.'''
        _OptionParser.__init__(self, usage=usage, version=version, epilog=epilog, 
                               formatter=CustomHelpFormatter(), 
                               conflict_handler=conflict_handler, **kwds)
        self.gui_mode = gui_mode
        
    def error(self, msg):
        if self.gui_mode:
            raise Exception(msg)
        _OptionParser.error(self, msg)
        
    def merge(self, parser):
        '''
        Add options from parser to self. In case of conflicts, confilicting options from
        parser are skipped.
        '''
        opts   = list(parser.option_list)
        groups = list(parser.option_groups)
        
        def merge_options(options, container):
            for opt in copy.deepcopy(options):
                if not self.has_option(opt.get_opt_string()):
                    container.add_option(opt)
                
        merge_options(opts, self)
        
        for group in groups:
            g = self.add_option_group(group.title)
            merge_options(group.option_list, g)
        
    def subsume(self, group_name, msg=''):
        '''
        Move all existing options into a subgroup named
        C{group_name} with description C{msg}.
        '''
        opts = [opt for opt in self.options_iter() if opt.get_opt_string() not in ('--version', '--help')]
        self.option_groups = []
        subgroup = self.add_option_group(group_name, msg)
        for opt in opts:
            self.remove_option(opt.get_opt_string())
            subgroup.add_option(opt)
        
    def options_iter(self):
        for opt in self.option_list:
            if str(opt).strip():
                yield opt
        for gr in self.option_groups:
            for opt in gr.option_list:
                if str(opt).strip():
                    yield opt
                
    def option_by_dest(self, dest):
        for opt in self.options_iter():
            if opt.dest == dest:
                return opt
    
    def merge_options(self, lower, upper):
        '''
        Merge options in lower and upper option lists into upper.
        Default values in upper are overriden by
        non default values in lower.
        '''
        for dest in lower.__dict__.keys():
            if not upper.__dict__.has_key(dest):
                continue
            opt = self.option_by_dest(dest)
            if lower.__dict__[dest] != opt.default and \
               upper.__dict__[dest] == opt.default:
                upper.__dict__[dest] = lower.__dict__[dest]
        

def load_library(name, cdll):
    if iswindows:
        return cdll.LoadLibrary(name)
    if isosx:
        name += '.dylib'
        if hasattr(sys, 'frameworks_dir'):
            return cdll.LoadLibrary(os.path.join(getattr(sys, 'frameworks_dir'), name))
        return cdll.LoadLibrary(name)
    return cdll.LoadLibrary(name+'.so')

def filename_to_utf8(name):
    '''Return C{name} encoded in utf8. Unhandled characters are replaced. '''
    if isinstance(name, unicode):
        return name.encode('utf8')
    codec = 'cp1252' if iswindows else 'utf8'
    return name.decode(codec, 'replace').encode('utf8')

def extract(path, dir):
    ext = os.path.splitext(path)[1][1:].lower()
    extractor = None
    if ext in ['zip', 'cbz', 'epub']:
        from calibre.libunzip import extract as zipextract
        extractor = zipextract
    elif ext in ['cbr', 'rar']:
        from calibre.libunrar import extract as rarextract
        extractor = rarextract
    if extractor is None:
        raise Exception('Unknown archive type')
    extractor(path, dir)

def get_proxies():
        proxies = {}
        if iswindows:
            try:
                winreg = __import__('_winreg')
                settings = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                          'Software\\Microsoft\\Windows'
                                          '\\CurrentVersion\\Internet Settings')
                proxy = winreg.QueryValueEx(settings, "ProxyEnable")[0]
                if proxy:
                    server = str(winreg.QueryValueEx(settings, 'ProxyServer')[0])
                    if ';' in server:
                        for p in server.split(';'):
                            protocol, address = p.split('=')
                            proxies[protocol] = address
                    else:
                        proxies['http'] = server
                        proxies['ftp'] =  server
                settings.Close()
            except Exception, e:
                print('Unable to detect proxy settings: %s' % str(e))
            if proxies:
                print('Using proxies: %s' % proxies)
        else:
            for q in ('http', 'ftp'):
                proxy =  os.environ.get(q+'_proxy', None)
                if not proxy: continue
                if proxy.startswith(q+'://'):
                    proxy = proxy[7:]
                proxies[q] = proxy
        return proxies


def browser(honor_time=False):
    opener = mechanize.Browser()
    opener.set_handle_refresh(True, honor_time=honor_time)
    opener.set_handle_robots(False)
    opener.addheaders = [('User-agent', 'Mozilla/5.0 (X11; U; i686 Linux; en_US; rv:1.8.0.4) Gecko/20060508 Firefox/1.5.0.4')]
    http_proxy = get_proxies().get('http', None)
    if http_proxy:
        opener.set_proxies({'http':http_proxy})
    return opener

def fit_image(width, height, pwidth, pheight):
    '''
    Fit image in box of width pwidth and height pheight. 
    @param width: Width of image
    @param height: Height of image
    @param pwidth: Width of box 
    @param pheight: Height of box
    @return: scaled, new_width, new_height. scaled is True iff new_widdth and/or new_height is different from width or height.  
    '''
    scaled = height > pheight or width > pwidth
    if height > pheight:
        corrf = pheight/float(height)
        width, height = floor(corrf*width), pheight
    if width > pwidth:
        corrf = pwidth/float(width)
        width, height = pwidth, floor(corrf*height)
    if height > pheight:
        corrf = pheight/float(height)
        width, height = floor(corrf*width), pheight
                            
    return scaled, int(width), int(height)

def get_lang():
    lang = locale.getdefaultlocale()[0]
    if lang is None and os.environ.has_key('LANG'): # Needed for OS X
        try:
            lang = os.environ['LANG']
        except:
            pass
    if lang:
        match = re.match('[a-z]{2,3}', lang)
        if match:
            lang = match.group()
    return lang

def set_translator():
    # To test different translations invoke as
    # LC_ALL=de_DE.utf8 program
    try:
        from calibre.translations.compiled import translations
    except:
        return
    lang = get_lang() 
    if lang:
        buf = None
        if os.access(lang+'.po', os.R_OK):
            buf = cStringIO.StringIO()
            make(lang+'.po', buf)
            buf = cStringIO.StringIO(buf.getvalue())
        elif translations.has_key(lang):
            buf = cStringIO.StringIO(translations[lang])
        if buf is not None:
            t = GNUTranslations(buf)
            t.install(unicode=True)
        
set_translator()

def sanitize_file_name(name):
    '''
    Remove characters that are illegal in filenames from name. 
    Also remove path separators. All illegal characters are replaced by
    underscores.
    '''
    return re.sub(r'\s', ' ', re.sub(r'["\'\|\~\:\?\\\/]|^-', '_', name.strip()))

def detect_ncpus():
    """Detects the number of effective CPUs in the system"""
    try:
        from PyQt4.QtCore import QThread
        ans = QThread.idealThreadCount()
        if ans > 0:
            return ans
    except:
        pass
    #for Linux, Unix and MacOS
    if hasattr(os, "sysconf"):
        if os.sysconf_names.has_key("SC_NPROCESSORS_ONLN"):
            #Linux and Unix
            ncpus = os.sysconf("SC_NPROCESSORS_ONLN")
            if isinstance(ncpus, int) and ncpus > 0:
                return ncpus
        else:
            #MacOS X
            try:
                return int(subprocess.Popen(('sysctl', '-n', 'hw.cpu'), stdout=subprocess.PIPE).stdout.read())
            except IOError: # Occassionally the system call gets interrupted
                try:
                    return int(subprocess.Popen(('sysctl', '-n', 'hw.cpu'), stdout=subprocess.PIPE).stdout.read())
                except IOError:
                    return 1
            except ValueError: # On some systems the sysctl call fails
                return 1
                
    #for Windows
    if os.environ.has_key("NUMBER_OF_PROCESSORS"):
        ncpus = int(os.environ["NUMBER_OF_PROCESSORS"]);
        if ncpus > 0:
            return ncpus
    #return the default value
    return 1


def launch(path_or_url):
    if os.path.exists(path_or_url):
        path_or_url = 'file:'+path_or_url
    QDesktopServices.openUrl(QUrl(path_or_url))
        
def relpath(target, base=os.curdir):
    """
    Return a relative path to the target from either the current dir or an optional base dir.
    Base can be a directory specified either as absolute or relative to current dir.
    """

    #if not os.path.exists(target):
    #    raise OSError, 'Target does not exist: '+target
    if target == base:
        raise ValueError('target and base are both: %s'%target)
    if not os.path.isdir(base):
        raise OSError, 'Base is not a directory or does not exist: '+base

    base_list = (os.path.abspath(base)).split(os.sep)
    target_list = (os.path.abspath(target)).split(os.sep)

    # On the windows platform the target may be on a completely different drive from the base.
    if iswindows and base_list[0].upper() != target_list[0].upper():
        raise OSError, 'Target is on a different drive to base. Target: '+repr(target)+', base: '+repr(base)

    # Starting from the filepath root, work out how much of the filepath is
    # shared by base and target.
    for i in range(min(len(base_list), len(target_list))):
        if base_list[i] != target_list[i]: break
    else:
        # If we broke out of the loop, i is pointing to the first differing path elements.
        # If we didn't break out of the loop, i is pointing to identical path elements.
        # Increment i so that in all cases it points to the first differing path elements.
        i+=1

    rel_list = [os.pardir] * (len(base_list)-i) + target_list[i:]
    return os.path.join(*rel_list)

def _clean_lock_file(file):
    try:
        file.close()
    except:
        pass
    try:
        os.remove(file.name)
    except:
        pass

class LockError(Exception):
    pass
class ExclusiveFile(object):
    
    def __init__(self, path, timeout=10):
        self.path = path
        self.timeout = timeout
        
    def __enter__(self):
        self.file  = open(self.path, 'a+b')
        self.file.seek(0)
        timeout = self.timeout
        if iswindows:
            name = ('Local\\'+(__appname__+self.file.name).replace('\\', '_'))[:201]
            while self.timeout < 0 or timeout >= 0:
                self.mutex = win32event.CreateMutex(None, False, name)
                if win32api.GetLastError() != winerror.ERROR_ALREADY_EXISTS: break
                time.sleep(1)
                timeout -= 1
        else:
            while self.timeout < 0 or timeout >= 0:
                try:
                    fcntl.lockf(self.file.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except IOError:
                    time.sleep(1)
                    timeout -= 1
        if timeout < 0 and self.timeout >= 0:
            self.file.close()
            raise LockError
        return self.file
                
    def __exit__(self, type, value, traceback):
        self.file.close()
        if iswindows:
            win32api.CloseHandle(self.mutex)

def singleinstance(name):
    '''
    Return True if no other instance of the application identified by name is running, 
    False otherwise.
    @param name: The name to lock.
    @type name: string 
    '''
    if iswindows:
        mutexname = 'mutexforsingleinstanceof'+__appname__+name
        mutex =  win32event.CreateMutex(None, False, mutexname)
        if mutex:
            atexit.register(win32api.CloseHandle, mutex)
        return not win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    else:
        global _lock_file
        path = os.path.expanduser('~/.'+__appname__+'_'+name+'.lock')
        try:
            f = open(path, 'w')
            fcntl.lockf(f.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
            atexit.register(_clean_lock_file, f)
            return True
        except IOError:
            return False
        
    return False

class Settings(QSettings):
    
    def __init__(self, name='calibre2'):
        QSettings.__init__(self, QSettings.IniFormat, QSettings.UserScope,
                           'kovidgoyal.net', name)
        
    def get(self, key, default=None):
        try:
            key = str(key)
            if not self.contains(key):
                return default
            val = str(self.value(key, QVariant()).toByteArray())
            if not val:
                return None
            return cPickle.loads(val)
        except:
            return default
    
    def set(self, key, val):
        val = cPickle.dumps(val, -1)
        self.setValue(str(key), QVariant(QByteArray(val)))
        
_settings = Settings()

if not _settings.get('rationalized'):
    __settings = Settings(name='calibre')
    dbpath = os.path.join(os.path.expanduser('~'), 'library1.db').decode(sys.getfilesystemencoding())
    dbpath = unicode(__settings.value('database path', 
                    QVariant(QString.fromUtf8(dbpath.encode('utf-8')))).toString())
    cmdline   = __settings.value('LRF conversion defaults', QVariant(QByteArray(''))).toByteArray().data()
    
    if cmdline:
        cmdline = cPickle.loads(cmdline)
        _settings.set('LRF conversion defaults', cmdline)
    _settings.set('rationalized', True)
    try:
        os.unlink(unicode(__settings.fileName()))
    except:
        pass
    _settings.set('database path', dbpath)

_spat = re.compile(r'^the\s+|^a\s+|^an\s+', re.IGNORECASE)
def english_sort(x, y):
    '''
    Comapare two english phrases ignoring starting prepositions.
    '''
    return cmp(_spat.sub('', x), _spat.sub('', y))

class LoggingInterface:
    
    def __init__(self, logger):
        self.__logger = logger
    
    def ___log(self, func, msg, args, kwargs):
        args = [msg] + list(args)
        for i in range(len(args)):
            if isinstance(args[i], unicode):
                args[i] = args[i].encode(preferred_encoding, 'replace')
                
        func(*args, **kwargs)
        
    def log_debug(self, msg, *args, **kwargs):
        self.___log(self.__logger.debug, msg, args, kwargs)
        
    def log_info(self, msg, *args, **kwargs):
        self.___log(self.__logger.info, msg, args, kwargs)
        
    def log_warning(self, msg, *args, **kwargs):
        self.___log(self.__logger.warning, msg, args, kwargs)
        
    def log_warn(self, msg, *args, **kwargs):
        self.___log(self.__logger.warning, msg, args, kwargs)
        
    def log_error(self, msg, *args, **kwargs):
        self.___log(self.__logger.error, msg, args, kwargs)
        
    def log_critical(self, msg, *args, **kwargs):
        self.___log(self.__logger.critical, msg, args, kwargs)
        
    def log_exception(self, msg, *args):
        self.___log(self.__logger.exception, msg, args, {})
        
        
def strftime(fmt, t=time.localtime()):
    '''
    A version of strtime that returns unicode strings.
    '''
    result = time.strftime(fmt, t)
    try:
        return unicode(result, locale.getpreferredencoding(), 'replace')
    except:
        return unicode(result, 'utf-8', 'replace')

def entity_to_unicode(match, exceptions=[], encoding='cp1252'):
    '''
    @param match: A match object such that '&'+match.group(1)';' is the entity.
    @param exceptions: A list of entities to not convert (Each entry is the name of the entity, for e.g. 'apos' or '#1234' 
    @param encoding: The encoding to use to decode numeric entities between 128 and 256. 
    If None, the Unicode UCS encoding is used. A common encoding is cp1252.
    '''
    ent = match.group(1)
    if ent in exceptions:
        return '&'+ent+';'
    if ent == 'apos':
        return "'"
    if ent.startswith(u'#x'):
        num = int(ent[2:], 16)
        if encoding is None or num > 255:
            return unichr(num)
        return chr(num).decode(encoding)
    if ent.startswith(u'#'):
        try:
            num = int(ent[1:])
        except ValueError:
            return '&'+ent+';'
        if encoding is None or num > 255:
            return unichr(num)
        try:
            return chr(num).decode(encoding)
        except UnicodeDecodeError:
            return unichr(num)
    try:
        return unichr(name2codepoint[ent])
    except KeyError:
        return '&'+ent+';'
 
if isosx:
    fdir = os.path.expanduser('~/.fonts')
    if not os.path.exists(fdir):
        os.makedirs(fdir)
    if not os.path.exists(os.path.join(fdir, 'LiberationSans_Regular.ttf')):
        from calibre.ebooks.lrf.fonts.liberation import __all__ as fonts
        for font in fonts:
            exec 'from calibre.ebooks.lrf.fonts.liberation.'+font+' import font_data'
            open(os.path.join(fdir, font+'.ttf'), 'wb').write(font_data)
            
