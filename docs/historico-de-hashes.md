# Histórico de hashes

Em 18/11/2026 o histórico do repositório foi reescrito para retirar uma linha de coautoria das mensagens de commit. O conteúdo, os autores e as datas de cada commit não mudaram, mas os hashes dos commits abaixo, e os dos seus descendentes, mudaram.

Os documentos em Markdown já citam os hashes novos. Os arquivos de medição (`.json`) e o instalador da versão 0.1.0 continuam com os antigos, porque registram o que foi medido e publicado naquele momento: use esta tabela para encontrá-los. A tag `v0.1.0` aponta para `a7f2b92`.

| Antes | Depois | Commit |
| --- | --- | --- |
| `25f64c7` | `e9054e8` | RFC-026 Search API: HTTP end-to-end (no migration) + testes + friend check |
| `ec54e51` | `25ba433` | Add real indexing+timing check for friend; README in PT-BR + first-clone fix |
| `856e1bd` | `52e9c01` | RFC-026 Search API: HTTP end-to-end (no migration) + testes |
| `d884697` | `2381333` | Documenta RFCs 001-021 e traduz RFCs 022-026 para portugues |
| `ed1bdf4` | `766f4c4` | Corrige os 5 erros pre-existentes de mypy na suite de testes |
| `148cecf` | `29a2eb7` | Propoe RFCs 027-030: identidade de dispositivo, data EXIF, jobs de indexacao e acesso a arquivo |
| `60903a5` | `a31379b` | Implementa RFC-027: dispositivos e identidade de volume |
| `29a992d` | `9c9eedc` | Implementa RFC-028: data de captura e filtro temporal |
| `22ab053` | `2b77b1f` | Implementa RFC-029: jobs de indexação e indexação seletiva |
| `725617b` | `8f89079` | Implementa RFC-030: acesso ao arquivo, thumbnails e revelação |
| `b10f021` | `e3099bb` | Implementa RFC-031: dispositivos, volumes e pastas pela API |
| `f0779cc` | `bd7d5b1` | Adiciona CLI unificado com subcomandos index e search |
| `b6bd867` | `0f34b2d` | Corrige loop de degeneração na tradução PT->EN de consultas |
| `914666a` | `063b92f` | Implementa RFC-032: geolocalização e busca por proximidade |
| `4309319` | `dbe829e` | Adiciona busca por imagem no CLI (search-image) |
| `5f08fac` | `05db15e` | Adiciona coordenadas e busca por proximidade no CLI |
| `e9c8b3a` | `ff4f77b` | Ignora artefatos do frontend Electron no git e no VS Code |
| `8b8d216` | `820ed6e` | Adiciona roadmap do SolidVision sem Docker |
| `322d946` | `b12f6da` | Corrige parsing de supported_extensions no .env e ajusta DATABASE_HOST local |
| `f5e1b87` | `d42f600` | Implementa RFC-033a: linha de base do PostgreSQL embarcado |
| `a67a96e` | `3600553` | Propoe RFC-033: PostgreSQL embarcado |
| `5a91b33` | `e95c7af` | Implementa RFC-033: PostgreSQL embarcado |
| `03445f7` | `c80bf32` | Corrige corrida entre terminate() e is_alive() no adaptador do PostgreSQL embarcado |
| `6155840` | `894226d` | Implementa RFC-034: configuração e diretórios do usuário |
| `e52372b` | `db0ad1f` | Propoe RFC-035: modelos offline |
| `f62365a` | `b9deba8` | Implementa RFC-035: modelos offline |
| `7c6cba8` | `ddcbc8b` | Atualiza o README: estado atual, como rodar e licença CC0 |
| `191bde0` | `e5f9a52` | Implementa RFC-036: launcher e supervisão de processos |
| `24dc081` | `43a4b50` | Implementa RFC-037: fundação e casca do frontend |
| `79e00e3` | `96e439b` | Implementa RFC-038: busca |
| `faf246f` | `b51595e` | Corrige o logoff do RFC-036: a parada no fim da sessão |
| `f379e01` | `527975b` | Registra o teste com a rede desligada do RFC-035 |
| `c376fab` | `b975e5d` | Merge remote-tracking branch 'origin/develop' into frontend foi consertado um problema de logoff que estava na develop |
| `471a3a1` | `e7079c8` | Implementa RFC-039: dispositivos e indexação |
| `1748ffc` | `6ded0e9` | Implementa RFC-040: mapa |
| `d9d9dea` | `e9d396e` | Logo no cabeçalho, barra lateral em índigo e painel de indexação em linha |
| `115e965` | `c651e05` | Propõe os RFCs 042 e 044 |
| `56187fd` | `124c08b` | Implementa RFC-043: busca por endereço no mapa |
| `34c3642` | `50bc59e` | Implementa RFC-044: fotos por localização, sem consulta textual |
| `1f7e96a` | `513c21f` | Implementa RFC-041: empacotamento e instalador |
| `6243bfb` | `67765bc` | RFC-042: coletor do Commons e página de legendas |
| `60062b4` | `aaf37bd` | RFC-042: 525 consultas de avaliação escritas e revisadas |
| `4e481e1` | `375c9ce` | Implementa RFC-045: leitura antecipada e decodificação reduzida |
| `9c68df9` | `bee28a9` | Implementa RFC-046: fotos ausentes do disco |
| `af1928a` | `2b95bcc` | RFC-046: o pen drive tirado no meio do job, medido |
| `f241b24` | `cd25d75` | RFC-042: conjunto de avaliação congelado e porta de entrada medida |
| `3c97707` | `385ede1` | Implementa RFC-047: léxico e marcadores de português na tradução |
| `086b454` | `dd72791` | RFC-047 §18: quatro formas pedidas depois do acervo real |
| `21e0af5` | `d360209` | Implementa RFC-042 nível A: matriz de ajuste na busca por texto |
| `31faf3d` | `db54397` | Implementa RFC-048: as conferências num comando só, com as versões fixadas |
| `8556d61` | `af4b9dd` | RFC-048 §17.12: o run -- acha o programa no PATH |
| `7584447` | `ec0bc34` | Implementa RFC-049: o primeiro workflow, frontend e análise estática |
| `6383c4c` | `51e922d` | RFC-049 §14 e §15: os números do runner |
| `9dbfcf6` | `a122ed2` | Traduz os comentários e docstrings de backend/app para o português |
| `3a24cb5` | `c6506f8` | Implementa RFC-050: a suíte padrão do backend no Windows |
| `2878133` | `834459b` | RFC-050 §16: a DACL pela API do Windows, e não pelo icacls |
| `0470336` | `b1eb47f` | Tira os placeholders de coleções e o .coverage versionado |
| `c88d905` | `538c789` | Roadmap de CI/CD: o corte de 11/11, com a release v0.1.0 à mão |
| `34c65c4` | `a7f2b92` | check_package.py separa as consultas que o RFC-047 mudou |
| `1025e86` | `5761b21` | RFC-050 §15 e §16: os números do runner |
| `ac4df0a` | `bdcc419` | Roadmap de CI/CD: os números da v0.1.0 e o que o corte achou |
| `9e81bfe` | `2da5c3c` | Relatórios da pipeline na RTX 4070 com o acervo real (Postgres embarcado) |
