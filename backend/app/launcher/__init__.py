"""O launcher: um comando que inicia, vigia e para o backend (RFC-036).

    python -m app.launcher                 inicia tudo e supervisiona
    python -m app.launcher --supervised    o mesmo, e para quando o stdin acaba
    python -m app.launcher status          o que está de pé, e onde
    python -m app.launcher stop            pede ao launcher de pé que pare

Um processo sem janela e sem `torch`, um por raiz de dados, que sabe iniciar,
vigiar e parar os outros três -- o cluster, a API e o job runner -- e mais
nada: não serve HTTP, não carrega modelo e não importa `torch`,
`transformers`, `fastapi` nem `uvicorn`
(`tests/test_launcher_imports_architecture.py` garante isso). Quem o inicia
o mantém vivo: um terminal, ou uma janela que segura o stdin dele (§4.1).

Uma raiz de composição, como `app.cli`. Nada no Domínio nem na Aplicação:
supervisionar processos é implantação, como o ciclo de vida do cluster
(RFC-033 §4.1).

    módulo          responsabilidade
    __main__        os comandos; o único módulo que lê os `Settings`
    supervisor      a máquina de estados: o início, o pronto, os reinícios, a parada
    children        um filho: criação, stdin, os leitores da saída dele
    instance        o lock, o arquivo de estado, o evento de parada
    events          o protocolo no stdout
    session_end     a janela oculta do logoff
"""
