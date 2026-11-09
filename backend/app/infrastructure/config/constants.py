"""Constantes do projeto inteiro para a camada de configuração."""

SUPPORTED_IMAGE_EXTENSIONS: tuple[str, ...] = (
    ".jpg",
    ".jpeg",
    ".png",
    ".tiff",
    ".bmp",
    ".webp",
)

DEFAULT_VECTOR_DISTANCE: str = "cosine"
PROJECT_ENCODING: str = "utf-8"

EXIT_CANNOT_START: int = 78
"""O código de saída de um programa que não inicia, e que reiniciar não resolve.

RFC-036 §6.1. A linha no stderr diz por quê, e não mudou desde o RFC-034 §5.5
e o RFC-035 §6.5; o código é o que deixa o launcher distinguir "o arquivo de
configuração é inválido" de "um defeito derrubou o processo" sem ler a linha.
78 é o `EX_CONFIG` de `sysexits.h`. O Windows não dá significado ao número, e
ele não colide com nada que Python ou uvicorn já usem: 1 para uma exceção não
tratada, 2 para um argumento ruim, 3 para o `STARTUP_FAILURE` do uvicorn, e
`0xC000013A` para um Ctrl+C que ninguém tratou.
"""
