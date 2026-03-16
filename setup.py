from setuptools import setup, find_packages

setup(
    name="nickname-common",
    version="0.1.0",
    description="Paquete compartido para los agentes de Nickname Management",
    author="Nickname Management SL.",
    author_email="diego@nickname.com",
    packages=find_packages(),
    python_requires=">=3.12",
    install_requires=[],
    extras_require={
        "odoo": [],       # xmlrpc.client es stdlib
        "hubspot": [],    # urllib es stdlib
        "all": [],
    },
)
