# -*- coding: utf-8 -*-

# Language extension for distutils Python scripts. Based on this concept:
# http://wiki.maemo.org/Internationalize_a_Python_application
from __future__ import print_function
from distutils import cmd
from distutils.command.build import build as _build
import glob
import os


class build_trans(cmd.Command):
	description = 'Compile .po files into .mo files'

	def initialize_options(self):
		pass

	def finalize_options(self):
		pass

	def run(self):
		s = os.path.join('po')
		lang_domains = glob.glob(os.path.join(s, '*.pot'))
		if len(lang_domains):
			for lang in os.listdir(s):
				if lang.endswith('.po'):
					src = os.path.join(s, lang)
					lang = lang[:-3]
					destdir = os.path.join('build', 'lib', 'SystemPlugins',
						'Signalfinder', 'locale', lang, 'LC_MESSAGES')
					if not os.path.exists(destdir):
						os.makedirs(destdir)
					for lang_domain in lang_domains:
						# Use os.path.basename to safely get the filename regardless of OS path separators
						lang_domain_filename = os.path.basename(lang_domain)
						dest = os.path.join(destdir, lang_domain_filename[:-3] + 'mo')
						print("Language compile %s -> %s" % (src, dest))
						# Quotes around paths ensure spaces in filenames don't break the command
						if os.system("msgfmt '%s' -o '%s'" % (src, dest)) != 0:
							raise Exception("Failed to compile: " + src)
		else:
			print("we got no domain -> no translation was compiled")


class build(_build):
	sub_commands = _build.sub_commands + [('build_trans', None)]

	def run(self):
		_build.run(self)


cmdclass = {
	'build': build,
	'build_trans': build_trans,
}
