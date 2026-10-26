# Servidor PTA

Servidor do Protocolo de Transferência de Arquivos descrito em [pta.pdf](pta.pdf),
feito em Python com sockets TCP.

A especificação, o cliente e os arquivos de exemplo vêm do
[repositório original do PTA](https://github.com/glaucogoncalves/pta).

## Executar

É necessário Python 3.10 ou superior, sem dependências externas.
Testado com Python 3.14.7, em Linux. Na raiz do repositório:

```bash
python3 pta-server/server.py
```

O servidor escuta em `0.0.0.0:11550`. Para escolher outra interface ou porta:

```bash
python3 pta-server/server.py --host 127.0.0.1 --port 11550
```

Use `Ctrl+C` para encerrar. Os caminhos de usuários e arquivos são relativos ao
script, então ele também pode ser executado de outro diretório.

## Protocolo

Cada pedido tem o formato `SEQ COMANDO [ARGUMENTO]`. A resposta repete a sequência
do pedido. O primeiro número é escolhido pelo cliente; os seguintes aumentam de um
em um. Comandos e nomes de usuários diferenciam maiúsculas de minúsculas.

| Pedido | Resposta |
| --- | --- |
| `0 CUMP user1` | `0 OK`, se o usuário estiver em `users.txt` |
| `1 LIST` | `1 ARQS quantidade nome1,nome2,...` |
| `2 PEGA nome1` | `2 ARQ tamanho conteúdo`, com tamanho em bytes |
| `3 TERM` | `3 OK`, seguido do fechamento da conexão |

`CUMP` deve ser o primeiro comando. Um usuário desconhecido ou um primeiro pedido
inválido recebe `NOK`, e a conexão é fechada. Depois da identificação, pedidos
inválidos e arquivos inexistentes recebem `NOK`, mantendo a conexão aberta.
Uma sequência fora de ordem é rejeitada sem avançar o número esperado.

Os usuários são lidos de `pta-server/users.txt` na inicialização. Os arquivos vêm de
`pta-server/files/`. O envio é binário e feito em blocos, incluindo arquivos vazios.
Cada conexão é atendida em uma thread, com uma operação por vez. Pedidos recebidos
durante uma resposta são descartados. Se `PEGA` receber mais nomes, só o primeiro
arquivo é considerado.

O PDF não define um terminador nem um tamanho para os pedidos. O cliente fornecido
envia cada pedido sem quebra de linha. Para atendê-lo, o servidor reúne os dados
até haver 50 ms sem novos bytes. Fragmentos com pausas maiores podem ser
interpretados como pedidos separados; sem enquadramento, não é possível distinguir
esses casos com certeza. Clientes próprios podem terminar cada pedido com LF ou
CRLF para delimitar a mensagem. As respostas não acrescentam quebra de linha.
Pedidos são limitados a 64 KiB; acima disso, a conexão recebe `NOK` e é encerrada.

## Testar

Com o servidor rodando, o cliente de exemplo pode ser executado em outro terminal:

```bash
python3 pta-client.py 127.0.0.1 11550 user1
```

O cliente salva o arquivo baixado no diretório atual. Ele escolhe um arquivo ao
acaso e tenta decodificar o conteúdo como texto; se escolher o PNG, pode falhar
mesmo com os bytes corretos.

Para executar os testes adicionais:

```bash
python3 -B -m unittest discover -s pta-server -v
```

Os testes iniciam servidores em portas locais livres; não precisam do servidor
principal rodando. Comparam todos os arquivos fornecidos byte a byte e verificam
erros de protocolo, sequências, listagens grandes, fragmentação e clientes
simultâneos. Os arquivos adicionais ficam em diretórios temporários.
