# Avisos de terceiros

> Gerado por `python scripts/third_party_notices.py` a partir do que foi materializado (`runtime/`, `frontend/node_modules`, os lockfiles). Não edite à mão: rode o script de novo quando um lockfile mudar. O `npm run dist` confere que este arquivo é o que o script gera, antes de montar o instalador.

O código e a documentação do SolidVision estão sob a CC0 1.0 (`LICENSE`). O instalador redistribui, junto com eles, os componentes abaixo, cada um sob a sua própria licença, que continua valendo. Os textos dessas licenças vão no programa instalado, nos lugares que cada seção diz; os que o programa não teria de outro jeito estão nos apêndices deste arquivo, que também vai no instalador (`resources\THIRD-PARTY-NOTICES.md`).

As imagens do dataset de demonstração (`backend/dataset/demo`) não vão no instalador e não são CC0: as licenças de cada uma estão no `manifest.json` de lá.

## Resumo

| Grupo | Componentes | Onde estão os textos, no programa instalado |
|---|---|---|
| Python 3.12.9 e os pacotes do app | 58 | `resources\runtime\python\LICENSE.txt` e a pasta `.dist-info` de cada pacote, em `resources\runtime\python\Lib\site-packages` |
| PostgreSQL, pgvector e as bibliotecas que eles usam | 16 | `resources\runtime\postgres\licenses` |
| Modelos de IA | 2 | Os cartões dos modelos, nos endereços da tabela; as licenças padrão no Apêndice B |
| Bibliotecas da interface | 12 | Apêndice A |
| Electron 44.9.0 e Chromium | 1 | `LICENSE.electron.txt` e `LICENSES.chromium.html`, na pasta do programa |
| Dados de mapa e serviços online | 3 | Esta página, seção *Dados e serviços* |

## Python

O Python 3.12.9 *embeddable* do python.org, sob a PSF License (`resources\runtime\python\LICENSE.txt`), com os DLLs do Visual C++ que ele e os pacotes carregam: `vc14_runtime` 14.51.36247, redistribuídos pela licença da Microsoft (`LicenseRef-MicrosoftVisualCpp2015-2022Runtime`), cujo texto está com o mesmo pacote na seção do PostgreSQL. Os pacotes, na versão exata do `scripts/python-win-64.lock`; um caminho de licença longo demais vira `licenses.zip` no instalador (RFC-041 §2.5).

| Pacote | Versão | Licença declarada | Textos, na pasta do pacote |
|---|---|---|---|
| alembic | 1.18.5 | MIT | `alembic-1.18.5.dist-info/licenses/LICENSE` |
| annotated-doc | 0.0.4 | MIT | `annotated_doc-0.0.4.dist-info/licenses/LICENSE` |
| annotated-types | 0.7.0 | MIT License | `annotated_types-0.7.0.dist-info/licenses/LICENSE` |
| anyio | 4.14.2 | MIT | `anyio-4.14.2.dist-info/licenses/LICENSE` |
| certifi | 2026.6.17 | MPL-2.0 | `certifi-2026.6.17.dist-info/licenses/LICENSE` |
| click | 8.4.2 | BSD-3-Clause | `click-8.4.2.dist-info/licenses/LICENSE.txt` |
| colorama | 0.4.6 | BSD License | `colorama-0.4.6.dist-info/licenses/LICENSE.txt` |
| fastapi | 0.139.2 | MIT | `fastapi-0.139.2.dist-info/licenses/LICENSE` |
| filelock | 3.32.3 | MIT | `filelock-3.32.3.dist-info/licenses/LICENSE` |
| fsspec | 2026.7.0 | BSD-3-Clause | `fsspec-2026.7.0.dist-info/licenses/LICENSE` |
| greenlet | 3.5.3 | MIT AND PSF-2.0 | `greenlet-3.5.3.dist-info/licenses/LICENSE`<br>`greenlet-3.5.3.dist-info/licenses/LICENSE.PSF` |
| h11 | 0.16.0 | MIT | `h11-0.16.0.dist-info/licenses/LICENSE.txt` |
| hf-xet | 1.6.0 | Apache-2.0 | `hf_xet-1.6.0.dist-info/licenses/LICENSE` |
| httpcore | 1.0.9 | BSD-3-Clause | `httpcore-1.0.9.dist-info/licenses/LICENSE.md` |
| httpx | 0.28.1 | BSD-3-Clause | `httpx-0.28.1.dist-info/licenses/LICENSE.md` |
| huggingface_hub | 1.27.0 | Apache-2.0 | `huggingface_hub-1.27.0.dist-info/licenses/LICENSE` |
| idna | 3.18 | BSD-3-Clause | `idna-3.18.dist-info/licenses/LICENSE.md` |
| Jinja2 | 3.1.6 | BSD License | `jinja2-3.1.6.dist-info/licenses/LICENSE.txt` |
| langdetect | 1.0.9 | MIT | `langdetect-1.0.9.dist-info/licenses/LICENSE`<br>`langdetect-1.0.9.dist-info/licenses/NOTICE` |
| Mako | 1.3.12 | MIT | `mako-1.3.12.dist-info/licenses/LICENSE` |
| markdown-it-py | 4.2.0 | MIT License | `markdown_it_py-4.2.0.dist-info/licenses/LICENSE`<br>`markdown_it_py-4.2.0.dist-info/licenses/LICENSE.markdown-it` |
| MarkupSafe | 3.0.3 | BSD-3-Clause | `markupsafe-3.0.3.dist-info/licenses/LICENSE.txt` |
| mdurl | 0.1.2 | MIT License | `mdurl-0.1.2.dist-info/LICENSE` |
| mpmath | 1.3.0 | BSD | `mpmath-1.3.0.dist-info/LICENSE` |
| networkx | 3.6.1 | BSD-3-Clause | `networkx-3.6.1.dist-info/licenses/LICENSE.txt` |
| numpy | 2.5.2 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | `numpy-2.5.2.dist-info/licenses/LICENSE.txt`<br>`numpy-2.5.2.dist-info/licenses/numpy/_core/include/numpy/libdivide/LICENSE.txt`<br>`numpy-2.5.2.dist-info/licenses/numpy/_core/src/common/pythoncapi-compat/COPYING`<br>`numpy-2.5.2.dist-info/licenses/numpy/_core/src/highway/LICENSE`<br>e mais 13 |
| packaging | 26.2 | Apache-2.0 OR BSD-2-Clause | `packaging-26.2.dist-info/licenses/LICENSE`<br>`packaging-26.2.dist-info/licenses/LICENSE.APACHE`<br>`packaging-26.2.dist-info/licenses/LICENSE.BSD` |
| pgvector | 0.5.0 | MIT | `pgvector-0.5.0.dist-info/licenses/LICENSE.txt` |
| pillow | 12.3.0 | MIT-CMU | `pillow-12.3.0.dist-info/licenses/LICENSE` |
| psycopg | 3.3.4 | LGPL-3.0-only | `psycopg-3.3.4.dist-info/licenses/LICENSE.txt` |
| psycopg-binary | 3.3.4 | LGPL-3.0-only | `psycopg_binary-3.3.4.dist-info/licenses/LICENSE.txt` |
| pydantic | 2.13.4 | MIT | `pydantic-2.13.4.dist-info/licenses/LICENSE` |
| pydantic_core | 2.46.4 | MIT | `pydantic_core-2.46.4.dist-info/licenses/LICENSE` |
| pydantic-settings | 2.14.2 | MIT | `pydantic_settings-2.14.2.dist-info/licenses/LICENSE` |
| Pygments | 2.20.0 | BSD-2-Clause | `pygments-2.20.0.dist-info/licenses/AUTHORS`<br>`pygments-2.20.0.dist-info/licenses/LICENSE` |
| python-dotenv | 1.2.2 | BSD-3-Clause | `python_dotenv-1.2.2.dist-info/licenses/LICENSE` |
| PyYAML | 6.0.3 | MIT | `pyyaml-6.0.3.dist-info/licenses/LICENSE` |
| regex | 2026.7.19 | Apache-2.0 AND CNRI-Python | `regex-2026.7.19.dist-info/licenses/LICENSE.txt` |
| rich | 15.0.0 | MIT | `rich-15.0.0.dist-info/licenses/LICENSE` |
| safetensors | 0.8.0 | Apache Software License | `safetensors-0.8.0.dist-info/licenses/LICENSE` |
| sentencepiece | 0.2.2 | Apache-2.0 | nenhum no pacote: o texto da licença está no Apêndice B |
| setuptools | 78.1.0 | MIT License | `setuptools-78.1.0.dist-info/licenses/LICENSE` |
| shellingham | 1.5.4 | ISC License | `shellingham-1.5.4.dist-info/LICENSE` |
| six | 1.17.0 | MIT | `six-1.17.0.dist-info/LICENSE` |
| SQLAlchemy | 2.0.51 | MIT | `sqlalchemy-2.0.51.dist-info/licenses/LICENSE` |
| starlette | 1.3.1 | BSD-3-Clause | `starlette-1.3.1.dist-info/licenses/LICENSE.md` |
| sympy | 1.14.0 | BSD | `sympy-1.14.0.dist-info/licenses/AUTHORS`<br>`sympy-1.14.0.dist-info/licenses/LICENSE` |
| tokenizers | 0.22.2 | Apache Software License | nenhum no pacote: o texto da licença está no Apêndice B |
| torch | 2.13.0+cpu | Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND BSD-3-Clause AND BSL-1.0 AND MIT | `torch-2.13.0+cpu.dist-info/licenses/LICENSE`<br>`torch-2.13.0+cpu.dist-info/licenses/third_party/FP16/LICENSE`<br>`torch-2.13.0+cpu.dist-info/licenses/third_party/FXdiv/LICENSE`<br>`torch-2.13.0+cpu.dist-info/licenses/third_party/NNPACK/LICENSE`<br>e mais 103 |
| torchvision | 0.28.0 | BSD | `torchvision-0.28.0.dist-info/LICENSE` |
| tqdm | 4.70.0 | MPL-2.0 AND MIT | `tqdm-4.70.0.dist-info/licenses/LICENCE` |
| transformers | 5.15.0 | Apache 2.0 License | `transformers-5.15.0.dist-info/licenses/LICENSE` |
| typer | 0.27.1 | MIT | `typer-0.27.1.dist-info/licenses/LICENSE` |
| typing_extensions | 4.16.0 | PSF-2.0 | `typing_extensions-4.16.0.dist-info/licenses/LICENSE` |
| typing-inspection | 0.4.2 | MIT | `typing_inspection-0.4.2.dist-info/licenses/LICENSE` |
| tzdata | 2026.3 | Apache-2.0 | `tzdata-2026.3.dist-info/licenses/LICENSE`<br>`tzdata-2026.3.dist-info/licenses/licenses/LICENSE_APACHE` |
| uvicorn | 0.51.0 | BSD-3-Clause | `uvicorn-0.51.0.dist-info/licenses/LICENSE.md` |

Dois pacotes têm licença LGPL-3.0, `psycopg` e `psycopg-binary`: são carregados como módulos à parte, e quem quiser pode trocá-los na pasta `site-packages`; o código-fonte está em https://github.com/psycopg/psycopg.

## PostgreSQL embarcado

Os pacotes do conda-forge do `scripts/postgres-win-64.lock`, com a licença que cada um declara. Os textos vêm dos próprios pacotes e ficam em `resources\runtime\postgres\licenses\<pacote>`. O código-fonte de cada um está na receita do conda-forge (a coluna *Origem*); a `libiconv`, sob a LGPL, é carregada como DLL à parte.

| Pacote | Versão | Licença declarada | Textos, em `licenses` | Origem |
|---|---|---|---|---|
| ca-certificates | 2026.7.22 | ISC | `ca-certificates-2026.7.22-h4c7d964_0/LICENSE` | https://github.com/conda-forge/ca-certificates-feedstock |
| icu | 78.3 | MIT | `icu-78.3-h5112557_2/LICENSE` | https://github.com/conda-forge/icu-feedstock |
| krb5 | 1.22.2 | MIT | `krb5-1.22.2-h719d79b_2/doc/notice.rst` | https://github.com/conda-forge/krb5-feedstock |
| libiconv | 1.18 | LGPL-2.1-only | `libiconv-1.18-hc1393d2_3/COPYING.LIB` | https://github.com/conda-forge/libiconv-feedstock |
| liblzma | 5.8.3 | 0BSD | `liblzma-5.8.3-hfd05255_1/COPYING`<br>`liblzma-5.8.3-hfd05255_1/COPYING.0BSD` | https://github.com/conda-forge/liblzma-feedstock |
| libpq | 16.15 | PostgreSQL | `libpq-16.15-h43e12c5_0/COPYRIGHT` | https://github.com/conda-forge/libpq-feedstock |
| libxml2-16 | 2.15.4 | MIT | `libxml2-16-2.15.4-h3cfd58e_1/Copyright` | https://github.com/conda-forge/libxml2-16-feedstock |
| libxml2 | 2.15.4 | MIT | `libxml2-2.15.4-he095d88_1/Copyright` | https://github.com/conda-forge/libxml2-feedstock |
| libzlib | 1.3.2 | Zlib | `libzlib-1.3.2-hfd05255_3/LICENSE` | https://github.com/conda-forge/libzlib-feedstock |
| openssl | 3.6.5 | Apache-2.0 | `openssl-3.6.5-hf411b9b_0/LICENSE.txt` | https://github.com/conda-forge/openssl-feedstock |
| pgvector | 0.8.6 | PostgreSQL | `pgvector-0.8.6-h2466b09_0/LICENSE` | https://github.com/conda-forge/pgvector-feedstock |
| postgresql | 16.15 | PostgreSQL | `postgresql-16.15-he837cf3_0/COPYRIGHT` | https://github.com/conda-forge/postgresql-feedstock |
| ucrt | 10.0.26100.0 | LicenseRef-MicrosoftWindowsSDK10 | `ucrt-10.0.26100.0-h57928b3_0/LICENSE.txt` | https://github.com/conda-forge/ucrt-feedstock |
| vc | 14.5 | BSD-3-Clause | nenhum: um metapacote, que não instala arquivos | https://github.com/conda-forge/vc-feedstock |
| vc14_runtime | 14.51.36247 | LicenseRef-MicrosoftVisualCpp2015-2022Runtime | `vc14_runtime-14.51.36247-habf1de7_41/LICENSE.DOCX`<br>`vc14_runtime-14.51.36247-habf1de7_41/LICENSE.RTF`<br>`vc14_runtime-14.51.36247-habf1de7_41/LICENSE.TXT` | https://github.com/conda-forge/vc14_runtime-feedstock |
| vcomp14 | 14.51.36247 | LicenseRef-MicrosoftVisualCpp2015-2022Runtime | `vcomp14-14.51.36247-habf1de7_41/LICENSE.DOCX`<br>`vcomp14-14.51.36247-habf1de7_41/LICENSE.RTF`<br>`vcomp14-14.51.36247-habf1de7_41/LICENSE.TXT` | https://github.com/conda-forge/vcomp14-feedstock |

## Modelos de IA

Os pesos, na revisão do `models.lock.json`, conferidos por `sha256` (RFC-035). Os repositórios dos pesos não trazem um arquivo de licença: a licença é a declarada no cartão de cada modelo, no endereço da tabela, e o texto padrão dela está no Apêndice B.

| Modelo | Revisão | Licença declarada | Origem |
|---|---|---|---|
| laion/CLIP-ViT-B-32-laion2B-s34B-b79K | 1a25a446 | MIT | https://huggingface.co/laion/CLIP-ViT-B-32-laion2B-s34B-b79K |
| Helsinki-NLP/opus-mt-ROMANCE-en | e9ca9975 | Apache-2.0 | https://huggingface.co/Helsinki-NLP/opus-mt-ROMANCE-en |

## Interface

As bibliotecas que o build da interface empacota (`frontend/dist`), as do `package-lock.json` que não são de desenvolvimento. O build junta o código e deixa os arquivos de licença para trás; por isso os textos estão aqui, no Apêndice A.

| Pacote | Versão | Licença | Origem |
|---|---|---|---|
| @fontsource-variable/inter | 5.3.0 | OFL-1.1 | git+https://github.com/fontsource/font-files.git |
| @remix-run/route-pattern | 0.22.1 | MIT | git+https://github.com/remix-run/remix.git |
| @tanstack/query-core | 5.104.3 | MIT | git+https://github.com/TanStack/query.git |
| @tanstack/react-query | 5.104.3 | MIT | git+https://github.com/TanStack/query.git |
| cookie-es | 3.1.1 | MIT | unjs/cookie-es |
| leaflet | 1.9.4 | BSD-2-Clause | git://github.com/Leaflet/Leaflet.git |
| openapi-fetch | 0.17.0 | MIT | https://github.com/openapi-ts/openapi-typescript |
| openapi-typescript-helpers | 0.1.0 | MIT | https://github.com/openapi-ts/openapi-typescript |
| react | 19.3.1 | MIT | https://github.com/react/react.git |
| react-dom | 19.3.1 | MIT | https://github.com/react/react.git |
| react-router | 8.4.0 | MIT | https://github.com/remix-run/react-router |
| scheduler | 0.28.0 | MIT | https://github.com/react/react.git |

## Electron

A janela é o Electron 44.9.0 (MIT), com o Chromium e as bibliotecas dele. O electron-builder põe os dois textos ao lado do `SolidVision.exe`: `LICENSE.electron.txt` e `LICENSES.chromium.html`.

## Dados e serviços

- **O contorno dos estados**, o fundo do mapa sem rede: Natural Earth 1:10m Cultural Vectors, Admin 1 - States, Provinces, versão 5.1.1, de https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_1_states_provinces.zip. Public domain: https://www.naturalearthdata.com/about/terms-of-use/ Filtrado e simplificado por `frontend/scripts/brazil-states.mjs`.
- **Os tiles do mapa**, com rede: do OpenStreetMap, carregados na hora e não distribuídos. © colaboradores do OpenStreetMap, sob a ODbL; a atribuição fica na tela (RFC-040).
- **A busca por endereço**, com rede e desligável (`ALLOW_ADDRESS_SEARCH`): o Nominatim do OpenStreetMap, pela política de uso dele (RFC-043). Dados © colaboradores do OpenStreetMap, sob a ODbL.

## Apêndice A — licenças das bibliotecas da interface

### @fontsource-variable/inter 5.3.0 (OFL-1.1)

```text
Copyright 2016 The Inter Project Authors (https://github.com/rsms/inter) Inter-Italic[opsz,wght].ttf: Copyright 2016 The Inter Project Authors (https://github.com/rsms/inter)

This Font Software is licensed under the SIL Open Font License, Version 1.1.
This license is copied below, and is also available with a FAQ at:
http://scripts.sil.org/OFL


-----------------------------------------------------------
SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007
-----------------------------------------------------------

PREAMBLE
The goals of the Open Font License (OFL) are to stimulate worldwide
development of collaborative font projects, to support the font creation
efforts of academic and linguistic communities, and to provide a free and
open framework in which fonts may be shared and improved in partnership
with others.

The OFL allows the licensed fonts to be used, studied, modified and
redistributed freely as long as they are not sold by themselves. The
fonts, including any derivative works, can be bundled, embedded,
redistributed and/or sold with any software provided that any reserved
names are not used by derivative works. The fonts and derivatives,
however, cannot be released under any other type of license. The
requirement for fonts to remain under this license does not apply
to any document created using the fonts or their derivatives.

DEFINITIONS
"Font Software" refers to the set of files released by the Copyright
Holder(s) under this license and clearly marked as such. This may
include source files, build scripts and documentation.

"Reserved Font Name" refers to any names specified as such after the
copyright statement(s).

"Original Version" refers to the collection of Font Software components as
distributed by the Copyright Holder(s).

"Modified Version" refers to any derivative made by adding to, deleting,
or substituting -- in part or in whole -- any of the components of the
Original Version, by changing formats or by porting the Font Software to a
new environment.

"Author" refers to any designer, engineer, programmer, technical
writer or other person who contributed to the Font Software.

PERMISSION & CONDITIONS
Permission is hereby granted, free of charge, to any person obtaining
a copy of the Font Software, to use, study, copy, merge, embed, modify,
redistribute, and sell modified and unmodified copies of the Font
Software, subject to the following conditions:

1) Neither the Font Software nor any of its individual components,
in Original or Modified Versions, may be sold by itself.

2) Original or Modified Versions of the Font Software may be bundled,
redistributed and/or sold with any software, provided that each copy
contains the above copyright notice and this license. These can be
included either as stand-alone text files, human-readable headers or
in the appropriate machine-readable metadata fields within text or
binary files as long as those fields can be easily viewed by the user.

3) No Modified Version of the Font Software may use the Reserved Font
Name(s) unless explicit written permission is granted by the corresponding
Copyright Holder. This restriction only applies to the primary font name as
presented to the users.

4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font
Software shall not be used to promote, endorse or advertise any
Modified Version, except to acknowledge the contribution(s) of the
Copyright Holder(s) and the Author(s) or with their explicit written
permission.

5) The Font Software, modified or unmodified, in part or in whole,
must be distributed entirely under this license, and must not be
distributed under any other license. The requirement for fonts to
remain under this license does not apply to any document created
using the Font Software.

TERMINATION
This license becomes null and void if any of the above conditions are
not met.

DISCLAIMER
THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT
OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE
COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL
DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM
OTHER DEALINGS IN THE FONT SOFTWARE.
```

### @remix-run/route-pattern 0.22.1 (MIT)

```text
MIT License

Copyright (c) 2025 Shopify Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### @tanstack/query-core 5.104.3 (MIT)

```text
MIT License

Copyright (c) 2021-present Tanner Linsley

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### @tanstack/react-query 5.104.3 (MIT)

```text
MIT License

Copyright (c) 2021-present Tanner Linsley

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### cookie-es 3.1.1 (MIT)

```text
MIT License

Cookie-es copyright (c) Pooya Parsa <pooya@pi0.io>

Cookie parsing based on https://github.com/jshttp/cookie
Copyright (c) 2012-2014 Roman Shtylman <shtylman@gmail.com>
Copyright (c) 2015 Douglas Christopher Wilson <doug@somethingdoug.com>

Set-Cookie parsing based on https://github.com/nfriedly/set-cookie-parser
Copyright (c) 2015 Nathan Friedly <nathan@nfriedly.com> (http://nfriedly.com/)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### leaflet 1.9.4 (BSD-2-Clause)

```text
BSD 2-Clause License

Copyright (c) 2010-2023, Volodymyr Agafonkin
Copyright (c) 2010-2011, CloudMade
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### openapi-fetch 0.17.0 (MIT)

```text
MIT License

Copyright (c) 2023 Drew Powers

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### openapi-typescript-helpers 0.1.0 (MIT)

```text
MIT License

Copyright (c) 2023 Drew Powers

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### react 19.3.1 (MIT)

```text
MIT License

Copyright (c) Meta Platforms, Inc. and affiliates.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### react-dom 19.3.1 (MIT)

```text
MIT License

Copyright (c) Meta Platforms, Inc. and affiliates.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### react-router 8.4.0 (MIT)

```text
MIT License

Copyright (c) React Training LLC 2015-2019
Copyright (c) Remix Software Inc. 2020-2021
Copyright (c) Shopify Inc. 2022-2023

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### scheduler 0.28.0 (MIT)

```text
MIT License

Copyright (c) Meta Platforms, Inc. and affiliates.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Apêndice B — textos padrão

Para os componentes que declaram uma licença e não trazem o texto dela: os pesos dos modelos e os pacotes Python marcados acima.

### Apache License 2.0

```text
Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/

   TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

   1. Definitions.

      "License" shall mean the terms and conditions for use, reproduction,
      and distribution as defined by Sections 1 through 9 of this document.

      "Licensor" shall mean the copyright owner or entity authorized by
      the copyright owner that is granting the License.

      "Legal Entity" shall mean the union of the acting entity and all
      other entities that control, are controlled by, or are under common
      control with that entity. For the purposes of this definition,
      "control" means (i) the power, direct or indirect, to cause the
      direction or management of such entity, whether by contract or
      otherwise, or (ii) ownership of fifty percent (50%) or more of the
      outstanding shares, or (iii) beneficial ownership of such entity.

      "You" (or "Your") shall mean an individual or Legal Entity
      exercising permissions granted by this License.

      "Source" form shall mean the preferred form for making modifications,
      including but not limited to software source code, documentation
      source, and configuration files.

      "Object" form shall mean any form resulting from mechanical
      transformation or translation of a Source form, including but
      not limited to compiled object code, generated documentation,
      and conversions to other media types.

      "Work" shall mean the work of authorship, whether in Source or
      Object form, made available under the License, as indicated by a
      copyright notice that is included in or attached to the work
      (an example is provided in the Appendix below).

      "Derivative Works" shall mean any work, whether in Source or Object
      form, that is based on (or derived from) the Work and for which the
      editorial revisions, annotations, elaborations, or other modifications
      represent, as a whole, an original work of authorship. For the purposes
      of this License, Derivative Works shall not include works that remain
      separable from, or merely link (or bind by name) to the interfaces of,
      the Work and Derivative Works thereof.

      "Contribution" shall mean any work of authorship, including
      the original version of the Work and any modifications or additions
      to that Work or Derivative Works thereof, that is intentionally
      submitted to Licensor for inclusion in the Work by the copyright owner
      or by an individual or Legal Entity authorized to submit on behalf of
      the copyright owner. For the purposes of this definition, "submitted"
      means any form of electronic, verbal, or written communication sent
      to the Licensor or its representatives, including but not limited to
      communication on electronic mailing lists, source code control systems,
      and issue tracking systems that are managed by, or on behalf of, the
      Licensor for the purpose of discussing and improving the Work, but
      excluding communication that is conspicuously marked or otherwise
      designated in writing by the copyright owner as "Not a Contribution."

      "Contributor" shall mean Licensor and any individual or Legal Entity
      on behalf of whom a Contribution has been received by Licensor and
      subsequently incorporated within the Work.

   2. Grant of Copyright License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      copyright license to reproduce, prepare Derivative Works of,
      publicly display, publicly perform, sublicense, and distribute the
      Work and such Derivative Works in Source or Object form.

   3. Grant of Patent License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      (except as stated in this section) patent license to make, have made,
      use, offer to sell, sell, import, and otherwise transfer the Work,
      where such license applies only to those patent claims licensable
      by such Contributor that are necessarily infringed by their
      Contribution(s) alone or by combination of their Contribution(s)
      with the Work to which such Contribution(s) was submitted. If You
      institute patent litigation against any entity (including a
      cross-claim or counterclaim in a lawsuit) alleging that the Work
      or a Contribution incorporated within the Work constitutes direct
      or contributory patent infringement, then any patent licenses
      granted to You under this License for that Work shall terminate
      as of the date such litigation is filed.

   4. Redistribution. You may reproduce and distribute copies of the
      Work or Derivative Works thereof in any medium, with or without
      modifications, and in Source or Object form, provided that You
      meet the following conditions:

      (a) You must give any other recipients of the Work or
          Derivative Works a copy of this License; and

      (b) You must cause any modified files to carry prominent notices
          stating that You changed the files; and

      (c) You must retain, in the Source form of any Derivative Works
          that You distribute, all copyright, patent, trademark, and
          attribution notices from the Source form of the Work,
          excluding those notices that do not pertain to any part of
          the Derivative Works; and

      (d) If the Work includes a "NOTICE" text file as part of its
          distribution, then any Derivative Works that You distribute must
          include a readable copy of the attribution notices contained
          within such NOTICE file, excluding those notices that do not
          pertain to any part of the Derivative Works, in at least one
          of the following places: within a NOTICE text file distributed
          as part of the Derivative Works; within the Source form or
          documentation, if provided along with the Derivative Works; or,
          within a display generated by the Derivative Works, if and
          wherever such third-party notices normally appear. The contents
          of the NOTICE file are for informational purposes only and
          do not modify the License. You may add Your own attribution
          notices within Derivative Works that You distribute, alongside
          or as an addendum to the NOTICE text from the Work, provided
          that such additional attribution notices cannot be construed
          as modifying the License.

      You may add Your own copyright statement to Your modifications and
      may provide additional or different license terms and conditions
      for use, reproduction, or distribution of Your modifications, or
      for any such Derivative Works as a whole, provided Your use,
      reproduction, and distribution of the Work otherwise complies with
      the conditions stated in this License.

   5. Submission of Contributions. Unless You explicitly state otherwise,
      any Contribution intentionally submitted for inclusion in the Work
      by You to the Licensor shall be under the terms and conditions of
      this License, without any additional terms or conditions.
      Notwithstanding the above, nothing herein shall supersede or modify
      the terms of any separate license agreement you may have executed
      with Licensor regarding such Contributions.

   6. Trademarks. This License does not grant permission to use the trade
      names, trademarks, service marks, or product names of the Licensor,
      except as required for reasonable and customary use in describing the
      origin of the Work and reproducing the content of the NOTICE file.

   7. Disclaimer of Warranty. Unless required by applicable law or
      agreed to in writing, Licensor provides the Work (and each
      Contributor provides its Contributions) on an "AS IS" BASIS,
      WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
      implied, including, without limitation, any warranties or conditions
      of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A
      PARTICULAR PURPOSE. You are solely responsible for determining the
      appropriateness of using or redistributing the Work and assume any
      risks associated with Your exercise of permissions under this License.

   8. Limitation of Liability. In no event and under no legal theory,
      whether in tort (including negligence), contract, or otherwise,
      unless required by applicable law (such as deliberate and grossly
      negligent acts) or agreed to in writing, shall any Contributor be
      liable to You for damages, including any direct, indirect, special,
      incidental, or consequential damages of any character arising as a
      result of this License or out of the use or inability to use the
      Work (including but not limited to damages for loss of goodwill,
      work stoppage, computer failure or malfunction, or any and all
      other commercial damages or losses), even if such Contributor
      has been advised of the possibility of such damages.

   9. Accepting Warranty or Additional Liability. While redistributing
      the Work or Derivative Works thereof, You may choose to offer,
      and charge a fee for, acceptance of support, warranty, indemnity,
      or other liability obligations and/or rights consistent with this
      License. However, in accepting such obligations, You may act only
      on Your own behalf and on Your sole responsibility, not on behalf
      of any other Contributor, and only if You agree to indemnify,
      defend, and hold each Contributor harmless for any liability
      incurred by, or claims asserted against, such Contributor by reason
      of your accepting any such warranty or additional liability.

   END OF TERMS AND CONDITIONS

   APPENDIX: How to apply the Apache License to your work.

      To apply the Apache License to your work, attach the following
      boilerplate notice, with the fields enclosed by brackets "[]"
      replaced with your own identifying information. (Don't include
      the brackets!)  The text should be enclosed in the appropriate
      comment syntax for the file format. We also recommend that a
      file or class name and description of purpose be included on the
      same "printed page" as the copyright notice for easier
      identification within third-party archives.

   Copyright [yyyy] [name of copyright owner]

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
```

### MIT License

Sem uma linha de copyright: a dos pesos do CLIP é a do cartão do modelo.

```text
Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
