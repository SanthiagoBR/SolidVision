# SolidVision
SolidVision 🚀
Plataforma Inteligente de Recuperação de Imagens para Acervos Fotográficos Baseada em Embeddings
O SolidVision é uma plataforma inteligente projetada para transformar a maneira como fotógrafos e empresas gerenciam grandes acervos locais
. Utilizando técnicas de Inteligência Artificial Multimodal, o sistema permite a recuperação de imagens através de descrições em linguagem natural, eliminando a dependência de catalogação manual, nomes de arquivos ou estruturas de pastas complexas
.
🌟 Principais Recursos
Busca Semântica: Pesquise por contexto visual (ex: "propriedade rural com lago" ou "casa com piscina") em vez de palavras-chave literais
.
Indexação Local: O sistema realiza a varredura automática de diretórios locais, integrando-se ao fluxo de trabalho do fotógrafo sem exigir o upload manual de arquivos para a nuvem
.
Independência de Domínio: Embora validado com acervos de fotografia aérea por drones, a arquitetura é flexível para fotografia de eventos, imobiliária e documentação técnica
.
Recuperação Zero-Shot: Capacidade de encontrar imagens mesmo para conceitos que o modelo nunca viu explicitamente durante o treinamento
.
🛠️ Tecnologias e Arquitetura
O projeto foi construído com foco em sustentabilidade e manutenibilidade, utilizando o estado da arte em IA e Engenharia de Software
:
IA & Modelos: CLIP (Contrastive Language-Image Pretraining) e SigLIP para extração de embeddings visuais e textuais
.
Banco de Dados Vetorial: PostgreSQL com a extensão pgvector para armazenamento e busca de alta performance por similaridade de cosseno
.
Backend: Desenvolvido em Python utilizando o framework FastAPI para orquestração da API REST
.
Infraestrutura: Arquitetura baseada em microsserviços e contêineres Docker
.
Padrões de Engenharia: Aplicação estrita de princípios SOLID, Desenvolvimento Orientado a Testes (TDD) e pipelines de CI/CD
.
🔄 Como Funciona (Pipeline de IA)
O funcionamento do sistema é dividido em quatro etapas principais
:
Varredura: O sistema identifica automaticamente arquivos de imagem em pastas selecionadas pelo usuário.
Extração: Modelos de atenção (Transformers) convertem cada imagem em um vetor numérico de alta dimensionalidade (embedding).
Persistência: Os vetores e metadados são salvos no banco vetorial, evitando o reprocessamento futuro.
Busca: Consultas textuais são convertidas em vetores e comparadas aos das imagens via similaridade de cosseno para retornar os resultados mais relevantes.
📊 Métricas de Avaliação
O desempenho do sistema é validado através de
:
mAP (mean Average Precision): Para medir a relevância dos resultados.
Latência de Busca: Garantindo respostas em poucos segundos em acervos locais.
Eficiência de Indexação: Tempo necessário para processar novos conjuntos de imagens.
✒️ Autor
Santhiago Chapiewski – santhiago.chapiewski@catolicasc.edu.br
.
📄 Licença
Este trabalho está licenciado sob a Creative Commons Attribution 4.0 International License
.
