from distutils.core import setup

setup(
    name='mlq',
    version='0.2.5',
    packages=['mlq', 'controller'],
    long_description=open('README.txt').read(),
    install_requires=open('requirements.txt').read(),
    include_package_data=True,
    url='https://github.com/clothifyai/mlq',
    author='Rachel Bastian',
    author_email='admin@clothify.ai'
)
