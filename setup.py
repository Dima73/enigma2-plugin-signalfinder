from distutils.core import setup
import setup_translate


setup(name = 'enigma2-plugin-systemplugins-signalfinder',
		version='1.0',
		author='Dima73',
		author_email='dima-73@inbox.lv',
		package_dir = {'SystemPlugins.Signalfinder': 'src'},
		packages=['SystemPlugins.Signalfinder'],
		package_data={'SystemPlugins.Signalfinder': ['update-plugin.sh', 'image/*.png']},
		description = 'Signal finder for DVB-S2 tuners',
		cmdclass = setup_translate.cmdclass,
	)

