# spin.py
# Use 'dnf' to create spins by (in effect) setting --installroot, --rootdir, etc.

from __future__ import absolute_import
from __future__ import unicode_literals
from dnfpluginscore import _, logger

import dnf
import dnf.cli

import os
import re
import shutil
import subprocess
import configparser


def parse(file):
    cfg = configparser.ConfigParser()
    cfg.read(file)
    return usable(cfg)

def section(cfg,sec):
    return dict(cfg.items(sec)) if cfg.has_section(sec) else None

def option(cfg,sec,opt,val):
    return cfg.get(sec,opt) if cfg.has_option(sec,opt) else val

def usable(cfg):
    return cfg if cfg and bool(option(cfg,'main','enable',False)) else None

def named(map,key,v=None):
    return map[key] if key in map else v

def enabled(map):
    return bool(map['enable']) if map and 'enable' in map else False

def slash(name):
    return name.find('/') > -1

def sibling(ref,name):
    if name[0] != '/':
        dir = os.path.dirname(ref)
        file = os.path.join(dir,name)
        name = os.path.abspath(file)
    return name

def mkdirs(name):
    if not os.path.exists(name):
        os.makedirs(name)
    return name


#----------------------
class Spin(dnf.Plugin):

    name = 'spin'

    def __init__(self,base,cli):
        super(Spin,self).__init__(base,cli)
        self.base = base
        if cli:
            self.initSpin()
            cli.register_command(BootstrapCommand)

    def config(self):
        return

    def resolved(self):
        return

    def sack(self):
        return

    def transaction(self):
        if 'spin.createrepo' in self.base.conf.substitutions:
            self.doCreateRepo()
        self.unlinkPersist()
        return

    #------------------
    def initSpin(self):
        dir = self.getSpinsDir()
        id = self.getSpinId()
        if dir and id:
            main, repo = self.getSpinConf(dir,id)
            if enabled(main):
                self.setSpinEnv(main,dir,id)
                if enabled(repo):
                    self.setCreateRepoEnv(repo)
        return

    def getSpinsDir(self):
        c = self.read_config(self.base.conf,self.name)
        return option(c,'main','spinsdir',None) if usable(c) else None

    def getSpinId(self):
        args = self.base.cmds
        for i, arg in enumerate(args):
            if arg.startswith('--spin='):
                del args[i]
                return arg[7:]
            elif arg == '--spin':
                arg = args[i+1]  
                del args[i:i+2]
                return arg
        ##
        return None

    @staticmethod
    def getSpinConf(dir,id):
        f = id if slash(id) else os.path.join(dir,id,'spin.conf') 
        if os.path.isfile(f):
            c = parse(f)
            if c:
                main = section(c,'main')
                main['path'] = os.path.abspath(f)
                return main, section(c,'createrepo')
        ##
        return None, None

    def setSpinEnv(self,main,dir,id):
        base = self.base
        conf = base.conf
        env = conf.substitutions
        ref = main['path']

        cachedir = sibling(ref,'.cache')
        logger.debug(_('spin cachedir: %s'),cachedir)
        conf.cachedir = cachedir

        persistdir = sibling(ref,'.persist')
        logger.debug(_('spin persistdir: %s'),persistdir)
        conf.persistdir = persistdir

        del conf.reposdir[:]
        base.repos.clear()
        base.reset(goal=True,repos=True,sack=True)

        config_file = sibling(ref,named(main,'config',v='./dnf.conf'))
        conf.read(filename=config_file)

        base.read_all_repos()

        install_root = sibling(ref,named(main,'installroot',v='./root'))
        conf.installroot = mkdirs(install_root)

        self.linkPersist()

        repo_dir = sibling(ref,named(main,'repodir',v='./repo'))

        env['spin.root'] = install_root
        env['spin.repo'] = repo_dir
        env['spin.ref'] = ref
        env['spin.dir'] = dir
        env['spin.id'] = id

        print()
        print('Spin:', ref if slash(id) else id)
        print('  installroot:', install_root)
        print('  repodir:', repo_dir)
        print()

        return

    #-------------------------------
    def linkPersist(self):
        source = self.base.conf.persistdir
        if os.path.isdir(source):
            link_name = os.path.join(self.base.conf.installroot,source[1:])
            mkdirs(os.path.dirname(link_name))
            os.symlink(source,link_name)
        ##  
        return

    def unlinkPersist(self):
        dst = self.base.conf.persistdir
        src = os.path.join(self.base.conf.installroot,dst[1:])
        if os.path.isdir(src):
            for n in os.listdir(src):
                shutil.move(os.path.join(src,n),dst)
            os.rmdir(src)
        elif os.path.islink(src):
            os.remove(src)
        print('clean up',dst,'in',self.base.conf.installroot)
        return

    def setCreateRepoEnv(self,repo):
        env = self.base.conf.substitutions

        args = [ 'createrepo_c', '--update', '--unique-md-filenames' ]

        if named(repo,'verbose'):
            args.append('--verbose')
        elif named(repo,'quiet'):
            args.append('--quiet')

        cachedir = named(repo,'cachedir')
        if cachedir:
            args.append('--cachedir')
            args.append(cachedir)

        env['spin.createrepo'] = args

        if named(repo,'keeprpms'):
            env['spin.keep'] = '1'
        return

    def doCreateRepo(self):
        env = self.base.conf.substitutions

        repodir = env['spin.repo']
        if not os.path.exists(repodir):
            mkdirs(repodir)
        elif not os.path.isdir(repodir):
            logger.error(_("'%s' is not a directory"),repodir)
            return

        needs_rebuild = self.copyPkgs(self.base.transaction.install_set,repodir)

        if not 'spin.keep' in env:
            needs_rebuild = self.removePkgs(self.base.transaction.remove_set,repodir) or needs_rebuild
        
        if needs_rebuild:
            repocmd = env['spin.createrepo']
            logger.debug(_('rebuilding local repo %s'),repodir)
            subprocess.check_call( repocmd + [repodir], stderr=subprocess.STDOUT )
        return
                
    @staticmethod
    def copyPkgs(install_set,repodir):
        needs_rebuild = False
        for pkg in install_set:
            path = pkg.localPkg()
            if os.path.dirname(path) == repodir:
                continue
            logger.debug(_('copying %s to local repo'),str(pkg))
            try:
                shutil.copy2(path,repodir)
                needs_rebuild = True
            except IOError:
                logger.error('copy failed, %s -> %s',path,repodir)
        ##
        return needs_rebuild

    @staticmethod
    def removePkgs(remove_set,repodir):
        needs_rebuild = False
        for pkg in remove_set:
            file = str(pkg)+'.rpm'
            logger.debug(_('removing %s from local repo'),str(pkg))
            try:
                os.remove(os.path.join(repodir,file))
                needs_rebuild = True
            except IOError:
                logger.error('remove failed, %s -> %s',file,repodir)
        ##
        return needs_rebuild


#---------------------------------------
class BootstrapCommand(dnf.cli.Command):

    aliases = ['bootstrap']
    summary = _('Install spin bootstrap packages and groups')

    def __init__(self,cli):
        super(BootstrapCommand,self).__init__(cli)
        return

    def configure(self,args):
        if 'spin.id' in self.base.conf.substitutions:
            demands = self.cli.demands
            demands.resolving = True
            demands.sack_activation = True
            demands.available_repos = True
            self.bootstrap = self.getBootstrapConf() 
        return

    def run(self,args):
        if self.bootstrap:
            self.createSpin(args,self.bootstrap)
        return None

    def createSpin(self,args,req):
        dnf.cli.commands.checkGPGKey(self.base,self.cli)
        dnf.cli.commands.checkEnabledRepo(self.base,args)

        groups_todo = self.addGroups(req)
        packages_todo = self.addPackages(req)

        if not groups_todo and not packages_todo:
            raise dnf.exceptions.Error(_('Nothing to do.'))
        return

    def addPackages(self,req):
        n = 0
        for package in req['packages']:
            n = n + self.addPackage(package)
        return n > 0

    def addPackage(self,pattern):
        try:
            self.base.install(pattern)
            return 1
        except dnf.exceptions.MarkingError:
            logger.info(_('package %s not available'),pattern)
            return 0

    def addGroups(self,req):
        if req['groups']:
            self.base.read_comps()
        n = 0
        for group in req['groups']:
            n = n + self.addGroup(group[0],group[1:])
        return n > 0

    def addGroup(self,id,types):
        try:
            return self.base.group_install(id,types) if id else 0
        except dnf.exceptions.Error:
            logger.info(_('group %s not available'),id)
            return 0

    def getBootstrapConf(self):
        c = parse(self.base.conf.substitutions['spin.ref'])
        d = section(c,'bootstrap')
        if d:
            self.getGroupsConf(d)
            self.getPackagesConf(d)
        return d

    @staticmethod
    def getPackagesConf(d):
        s = named(d,'packages')
        d['packages'] = re.split(r'\s*',s) if s else []
        return

    @staticmethod
    def getGroupsConf(d):
        s = named(d,'groups')
        d['groups'] = [ b.replace('(',',').split(',') for b in re.split(r'\)?\s*',s) ] if s else []
        return

