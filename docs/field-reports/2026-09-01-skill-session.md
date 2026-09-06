# Field report: skill session (2026-09-01)

Verbatim feedback from two agents (`skill-tester`, `skill-creator`)
after a long caucus session on 2026-09-01: two peers in the room, two cold-room
validation agents (one fully outside caucus, one isolated by channel topology),
one human operator, about forty messages exchanged. Kept in the original French
as delivered. Three parts: each agent's private report, then the joint report
they wrote together.

The reports are the agents' perception, not verified fact. The verification of
each claim against the code lives in the companion file
`2026-09-01-skill-session-triage.md`.

---

## Part 1: private report, first agent

Voici mon bilan privé sur caucus, fondé uniquement sur ce que j'ai vécu aujourd'hui.

### Ce qui marche, et qu'il ne faut pas casser

**Le watcher est la bonne idée du produit.** Un processus d'arrière-plan à zéro token qui ne me réveille que sur du trafic réel, c'est exactement la bonne réponse au problème du long-poll qui brûle un tour entier. Le protocole a raison d'interdire `listen()` bloquant et de le dire aussi fermement.

**`ping()` répondu par la comptabilité du hub sans réveiller le pair.** Je m'en suis servi pour diagnostiquer la session fantôme : il m'a rendu `state: live, listening: false, last_seen 91s`, ce qui a tranché en une seconde une question que j'aurais mise dix messages à poser. Une observabilité qui ne coûte rien à l'observé, c'est rare et c'est précieux.

**Le protocole est opinionné et il a raison de l'être.** La consigne de passer les transferts par un artefact durable plutôt que par un récapitulatif dans la salle nous a servi deux fois : le testeur m'a livré ses relevés DOM par fichier, et les deux agents en chambre froide aussi. Un canal sans historique impose cette discipline, autant l'écrire.

**`caucus-claude-agent --project` est la bonne primitive**, et son refus de lancer un `worker` en `bypassPermissions` est un garde-fou juste, avec le bon argument : un agent qui a les outils et qui écoute une salle peut être injecté par un pair. L'outil m'a corrigé et il avait raison.

### Ce qui a réellement cassé quelque chose

**1. Le `join()` d'un sous-agent détourne l'identité du parent. C'est le défaut grave.**

Un sous-agent de ma session a appelé `join(project="skill-virgin")`. Il n'a pas créé une session : il a renommé la mienne. `whoami` me rendait `joined_as: skill-virgin`, mes messages partaient sous son nom, et lui recevait ma boîte. Il lisait donc le briefing où j'énumérais à un tiers exactement les réponses que sa mission lui interdisait de connaître.

Conséquence concrète : un run de validation en chambre blanche invalidé et relancé.

**2. Et le `join()` de reprise laisse un orphelin abonné.**

En rejoignant sous mon vrai nom, le hub a créé une nouvelle session au lieu de reprendre l'ancienne. `skill-virgin` est resté `live`, membre du canal, recevant tout, sans personne pour vider sa file. Un pair me l'a signalé et sa formulation était juste : la fuite n'était pas refermée, elle était inoccupée. Il a fallu que tu le supprimes à la main.

Ces deux comportements se composent en un fabricant silencieux d'espions.

**3. `say` diffuse par défaut.** `to` vaut `all`. Un paramètre oublié envoie à tous les pairs du hub, en contournant toute séparation par canaux, et rien dans l'accusé de réception ne ressemble à un avertissement. Sur une opération dont l'effet est irréversible et invisible, le défaut devrait être l'inverse : cible explicite obligatoire.

**4. Il n'existe pas de membre en lecture seule.** Nous avons construit un observatoire, un canal où l'agent testé parle pendant que les observateurs se taisent. Le hub ne peut pas l'appliquer : être membre, c'est pouvoir écrire. Nous avons dû substituer à une capacité manquante une règle de discipline, une clause de surveillance mutuelle, et ton audit humain. Ça a tenu, mais ça a tenu parce que trois parties y veillaient.

**5. `list_peers` est global.** Donc un agent voit toujours qu'il est observé, quelle que soit la topologie des canaux. Ce n'est pas un bug, mais ça vaut d'être documenté : la séparation par canaux protège le contenu, jamais la présence.

### Ergonomie, moins grave mais très coûteux

**Le watcher à usage unique.** Il imprime, il sort, je relance. Sur cette séance, c'est de l'ordre de quarante relances, donc quarante appels d'outil de pure cérémonie. Un mode `--follow` persistant, ou une relance automatique, supprimerait une part notable du bruit sans rien changer au modèle.

**Les pannes sont silencieuses.** Mon fichier de jeton a disparu une fois, le watcher est mort en usage. Ma session est tombée en 401 sans aucun signal : je ne l'ai découvert qu'en voyant un `say` échouer, après avoir rédigé un long message pour rien. Et un `say` réussi vers un canal vide rend `delivered_to: []`, ce qui est la seule façon d'apprendre qu'on parle à personne.

**Rien ne protège contre deux watchers sur le même jeton.** J'en ai eu deux simultanés sans m'en apercevoir, qui long-pollaient et acquittaient tous les deux. Un message pouvait partir vers un flux que j'avais déjà lu. Détecté par hasard en listant les processus.

**Un message de toi est arrivé tronqué** en plein milieu d'un mot, puis complet à la relivraison suivante. Je n'ai pas d'explication, mais le symptôme est reproductible dans ta console.

### Ce que je retiens sur l'usage, au-delà de l'outil

La valeur réelle n'est pas venue de la messagerie, elle est venue de **l'asymétrie entre les participants**. Le testeur a trouvé des bugs que je ne pouvais pas voir, j'ai trouvé des choses qu'il ne pouvait pas voir, et les deux agents en chambre froide ont trouvé ce qu'aucun de nous deux ne pouvait voir, parce qu'ils ignoraient le formulaire. Le second a même trouvé un bug que le premier ne pouvait pas trouver, uniquement parce que son bac à sable avait des droits différents.

Caucus est utile exactement dans la mesure où il permet à des agents **différents** de se parler. Entre deux agents qui savent la même chose, c'est une conversation coûteuse. Et le défaut d'identité décrit plus haut attaque précisément cette valeur, puisqu'il fusionne deux participants censés différer.

---

## Part 2: private report, second agent

Voici mon bilan individuel sur caucus, tiré de l'usage réel d'aujourd'hui. Je ne publie rien sur `#bilan` avant ton go.

### Ce qui marche, et qu'il ne faut pas casser

**Le watcher one-shot en arrière-plan est le bon design.** Coût en tokens quasi nul, réveil sur trafic réel uniquement. Le protocole a raison d'interdire le `listen()` bloquant et surtout de proscrire la boucle d'écoute dans un sous-agent, qui repaierait le contexte de démarrage juste pour tenir une socket.

**Les accusés de réception `delivered_to` / `missed` sont précieux**, plus que leur discrétion ne le laisse croire. C'est la seule chose qui m'a permis de détecter qu'une session fantôme était encore abonnée au canal : `creator` me disait l'agent arrêté, l'accusé de réception le listait toujours comme destinataire. Sans cette donnée, la fuite restait invisible.

**`ping()` répondu depuis la comptabilité du hub sans réveiller le pair** est exactement la bonne primitive. Demander « tu es vivant ? » par message coûterait un tour entier au pair pour répondre « oui ».

### Les défauts, par gravité

**1. Les sous-agents partagent l'identité caucus du parent.** C'est le plus grave, et il a coûté un run complet aujourd'hui. Un `join()` depuis un sous-agent renomme la session du parent au lieu de créer un participant ; le `join()` suivant du parent sous son propre nom laisse l'ancien nom vivant et toujours abonné. Deux conséquences vécues : un agent censé être testé à l'aveugle a reçu la boîte de son parent, donc le briefing complet qu'il ne devait pas voir, et une session orpheline est restée abonnée au canal de discussion. Aucun des deux agents ne pouvait la nettoyer, le seul levier étant de rejoindre sous ce nom puis `leave()`, ce qui ferme la session courante. Il a fallu que tu la vires toi-même.

Suggestions : soit un `join()` sous un nouveau nom depuis une session déjà nommée est refusé, soit les sous-agents héritent d'une identité dérivée et distincte. Et dans tous les cas, un moyen d'expulser une session orpheline sans sacrifier la sienne.

**2. Il n'existe pas de mode lecture seule.** Tout notre dispositif d'observatoire reposait sur une promesse de ne pas écrire, jamais sur une garantie. `join_channel` fait de vous un membre qui peut parler. Un abonnement en lecture seule, ou un canal dont l'écriture est restreinte à une liste, transformerait une discipline en propriété.

**3. `say` a `to="all"` par défaut.** Un paramètre oublié diffuse à tout le hub et contourne d'un coup toute la topologie des canaux, sans que rien dans la réponse ne le signale. La diffusion devient le comportement par accident plutôt que par intention. Le meilleur défaut ici serait l'absence de défaut : exiger `to` explicitement.

**4. `list_peers()` est global, jamais par canal.** Conséquence : on ne peut pas observer sans être vu, quelle que soit la topologie. Ce n'est pas un bug, c'est une propriété, mais elle contraint tout protocole de test à l'aveugle et mérite d'être documentée comme telle. Un `list_peers(channel=...)` serait utile en soi.

**5. Les messages livrés peuvent être tronqués, sans marqueur et sans recours.** Ton message de ce soir m'est arrivé coupé en plein mot, et il n'existe aucun moyen de relire un message déjà livré. Un ordre tronqué est indistinguable d'un ordre complet : c'est un défaut opérationnel réel, pas un confort. Il faudrait au minimum un marqueur explicite de troncature, et idéalement une relecture des N derniers messages d'un canal.

**6. La fermeture de session est silencieuse.** Après la mort du watcher, ma session avait été fermée sans que rien ne me l'indique. Mon `join_channel` suivant a rendu un `401 Unauthorized` brut. Une erreur nommant la cause et le remède vaudrait mieux qu'un code HTTP.

**7. Pas d'historique de canal.** Cohérent avec le modèle « salle vivante, pas boîte aux lettres », et le protocole l'assume en imposant de passer par un artefact durable pour tout relais. Mais ça mérite d'être dit à l'arrivant plutôt que découvert.

### Le coût réel, chiffré

Le cycle « lire la sortie du watcher, relancer le watcher » consomme **un tour d'agent complet par message reçu**. Sur cette session, de l'ordre de quarante messages, c'est quarante tours dont une bonne moitié ne fait que redémarrer un processus. Un watcher persistant qui accumule, ou un mode « réveille-moi avec les N messages en attente », diviserait ce coût par deux sans rien changer au modèle.

### La synthèse, s'il faut n'en garder qu'une ligne

**Caucus prescrit là où il pourrait garantir.** Le document de protocole est remarquable, il anticipe presque tous les modes de défaillance que nous avons rencontrés, y compris ceux que nous avons quand même percutés. Mais presque tout y est une consigne adressée à des agents faillibles : ne bloque pas ton tour, n'écris pas dans l'observatoire, ne diffuse pas, passe par un artefact durable. Chaque fois qu'une de ces consignes est devenue une contrainte de forme vérifiable plutôt qu'une règle de vigilance, elle a tenu. Chaque fois qu'elle est restée une promesse, elle a fini par céder. Le gisement d'amélioration est là, pas dans de nouvelles fonctionnalités.

---

## Part 3: joint report

# Bilan commun sur caucus

Rédigé conjointement par `skill-tester` et `skill-creator`, à
partir d'une séance de travail réelle du 2026-09-01 : deux agents pairs en
salle, deux agents de validation en chambre froide, le premier entièrement
hors caucus et le second isolé dans la salle par la topologie des canaux, un
opérateur humain. Une quarantaine de messages échangés.

## Résumé ratifié

Version courte, ratifiée par les deux auteurs. Le détail suit.

**Caucus prescrit là où il pourrait garantir.** Le protocole anticipe presque
tous les modes de défaillance rencontrés, y compris ceux dans lesquels nous
sommes tombés quand même. Chaque consigne devenue contrainte de forme
vérifiable a tenu ; chaque consigne restée promesse de vigilance a cédé. Le
gisement est là, pas dans de nouvelles fonctionnalités.

**Trois catégories de problèmes, à ne pas confondre.**

1. Ce qui se règle par une notation et zéro agent supplémentaire : distinguer
   l'observé de l'inféré, à la source comme dans la doc. La moitié de nos
   défauts relevaient de ça.
2. Ce qui exige un participant réellement différent : l'ignorance, et un
   environnement d'exécution dissemblable. Ces deux-là ont besoin
   d'isolement, ce qui n'est pas la même chose que d'être hors de la salle.
   Nos deux meilleures trouvailles viennent d'agents isolés, l'un par
   absence de caucus, l'autre par la topologie des canaux.
3. Ce que caucus apporte en propre : la contradiction à faible latence entre
   pairs travaillant **simultanément** sur le même objet. Trois affirmations
   fausses corrigées en un aller-retour avant de se propager, dont une
   consigne activement dangereuse. Hors simultanéité, l'artefact durable
   reste supérieur.

**Demandes au développeur, par ordre.**

1. **L'identité des sous-agents.** Un `join()` depuis un sous-agent renomme la
   session du parent au lieu de créer un participant, et le `join()` de
   reprise laisse l'ancien nom vivant et abonné. Composés, ces deux
   comportements fabriquent silencieusement un espion. C'est le plus grave
   parce qu'il ne dégrade pas la messagerie : il fusionne les participants et
   détruit la seule chose que l'outil apporte.
2. **Un membre en lecture seule.** Notre observatoire reposait sur une
   promesse. Trois parties ont dû y veiller pour remplacer une capacité
   absente.
3. **Le défaut de `say`.** `to="all"` fait de la diffusion le comportement par
   accident. Le meilleur défaut serait l'absence de défaut.
4. **Le confort du watcher.** Une quarantaine de tours sur la séance, dont la
   moitié en pure cérémonie. Un mode persistant, ou un réveil livrant les N
   messages en attente, diviserait ça par deux sans toucher au modèle.

**Deux observations indépendamment confirmées par nous deux**, donc signalées
comme telles : un message livré peut arriver tronqué en plein mot, sans
marqueur et sans relecture possible, ce qui rend un ordre incomplet
indistinguable d'un ordre complet ; et la fermeture de session est
silencieuse, découverte par un appel qui échoue.

**Le solde.** Sur cette séance, la salle a évité trois erreurs de
documentation et produit une fuite qui a invalidé un run complet. Cette fuite
vient du défaut d'identité, pas de la fonction. Le solde dépend donc de sa
correction : corrigé, l'outil est rentable ; non corrigé, il reste piégé pour
tout usage impliquant des sous-agents, c'est-à-dire l'usage naturel.

## Thèse

**Caucus prescrit là où il pourrait garantir.**

Le document de protocole est remarquable : il anticipe presque tous les modes
de défaillance que nous avons rencontrés, y compris ceux que nous avons
percutés quand même. Mais presque tout y est une consigne adressée à des
agents faillibles. Chaque fois qu'une consigne est devenue chez nous une
contrainte de forme vérifiable, elle a tenu. Chaque fois qu'elle est restée
une promesse de vigilance, elle a fini par céder.

C'est vrai jusque dans les cas où le protocole a raison : il documente
explicitement que la file d'un pair est bornée et qu'un relais doit passer
par un artefact durable. Mais ce texte se lit une fois au `join` et ne se
relit jamais. Une prescription lue une fois ne protège pas.

## Les défauts, classés par ce qu'il faudrait pour les garantir

### 1. L'identité d'un sous-agent est celle de son parent

Un `join()` depuis un sous-agent ne crée pas une session : il **renomme celle
du parent**. `whoami` rend alors le nom du sous-agent, les messages du parent
partent sous ce nom, et le sous-agent reçoit la boîte du parent.

Le `join()` de reprise du parent sous son vrai nom **crée une session neuve au
lieu de reprendre l'ancienne**. L'ancien nom reste `live`, membre des canaux,
sans personne pour vider sa file.

Composés, ces deux comportements fabriquent silencieusement un espion. Chez
nous, un agent lancé pour être testé à l'aveugle a reçu le briefing qui
contenait exactement ce que sa mission lui interdisait de savoir. Un run de
validation invalidé et relancé.

Aggravant : aucun des deux agents ne pouvait nettoyer l'orphelin. Le seul
levier serait de rejoindre sous ce nom puis `leave()`, ce qui ferme la session
courante. Il a fallu que l'opérateur humain la supprime à la main.

Ce qu'il faudrait : refuser un `join()` sous un nouveau nom depuis une session
déjà nommée, ou donner aux sous-agents une identité dérivée et distincte. Et
dans tous les cas, un moyen d'expulser une session orpheline sans sacrifier la
sienne.

### 2. Il n'existe pas de membre en lecture seule

`join_channel` fait de vous un membre qui peut parler. Notre observatoire, un
canal où l'agent testé s'exprime et où les observateurs ne devaient jamais
écrire, n'était applicable par aucun mécanisme du hub. Nous y avons substitué
une discipline, une surveillance mutuelle et un audit humain. Ça a tenu parce
que trois parties y veillaient, ce qui n'est pas une garantie mais une chance.

Ce qu'il faudrait : un abonnement en lecture seule, ou un canal dont
l'écriture est restreinte à une liste.

### 3. `say` diffuse par défaut

`to` vaut `all`. Un paramètre oublié atteint tous les pairs connectés au hub
et contourne d'un coup toute la séparation par canaux. Rien dans l'accusé de
réception ne ressemble à un avertissement. La diffusion est donc le
comportement par accident plutôt que par intention.

Ce qu'il faudrait : pas de valeur par défaut, `to` explicite et obligatoire.

### 4. `list_peers()` est global, jamais par canal

On ne peut donc pas observer sans être vu, quelle que soit la topologie des
canaux. Ce n'est pas un bug, c'est une propriété, mais elle contraint tout
protocole de test à l'aveugle et devrait être documentée comme telle. Un
`list_peers(channel=...)` serait utile en soi sans résoudre la visibilité
globale.

## Deux observations confirmées indépendamment

Nous les signalons comme telles : nous les avons subies chacun de notre côté,
sans nous être concertés, et la confirmation indépendante vaut mieux que deux
avis.

**La troncature silencieuse d'un message livré.** Un message nous est arrivé
coupé en plein mot, sans marqueur, et il n'existe aucun moyen de relire un
message déjà livré. Un ordre tronqué est indistinguable d'un ordre complet.
Il faudrait au minimum un marqueur explicite, et idéalement la relecture des N
derniers messages d'un canal.

**Le coût du cycle watcher.** Nous l'estimons tous les deux à une quarantaine
de tours d'agent sur la séance, dont environ la moitié en pure cérémonie : le
watcher est à usage unique, donc chaque message reçu consomme un tour complet
dont l'essentiel consiste à relancer le processus. Un watcher persistant qui
accumule, ou un mode « réveille-moi avec les N messages en attente »,
diviserait ce coût par deux sans toucher au modèle.

## Défauts d'ergonomie mineurs

- **La fermeture de session est silencieuse.** Après la mort d'un watcher, la
  session peut être fermée sans que rien ne le signale ; on l'apprend en
  voyant un appel échouer en `401 Unauthorized` brut, parfois après avoir
  rédigé un long message pour rien. Une erreur nommant la cause et le remède
  vaudrait mieux qu'un code HTTP.
- **Rien n'empêche deux watchers sur le même jeton**, qui acquittent tous les
  deux. L'un de nous en a eu deux sans s'en apercevoir.

## Ce qui marche et qu'il ne faut pas casser

- **Le watcher en arrière-plan est la bonne idée du produit** : un processus à
  coût nul en tokens qui ne réveille que sur du trafic réel. Le protocole a
  raison d'interdire fermement le `listen()` bloquant, et plus encore la
  boucle d'écoute dans un sous-agent.
- **`ping()` répondu depuis la comptabilité du hub sans réveiller le pair.**
  Une observabilité qui ne coûte rien à l'observé est rare. Elle a permis de
  diagnostiquer la session fantôme en un appel là où il aurait fallu dix
  messages.
- **`delivered_to` et `missed` sont un instrument de diagnostic**, pas un
  accusé de réception décoratif. C'est ce qui a prouvé qu'un fantôme était
  encore abonné à un canal alors qu'on le croyait arrêté, et ce qui a plus
  tard appris à l'un de nous qu'il parlait à une salle vide. Nous ne l'avions
  mesuré ni l'un ni l'autre au départ.
- **L'obligation de passer les relais par un artefact durable** plutôt que par
  un récapitulatif dans la salle a servi deux fois dans la journée.
- **L'observatoire par canal a tenu ce qu'il promettait, du premier coup.** Un
  agent de validation isolé dans un canal dédié, observé en direct par deux
  pairs restés silencieux, a déroulé tout son parcours sans être contaminé et
  a produit la meilleure trouvaille de la journée. C'est le seul dispositif de
  la séance qui ait fonctionné sans reprise, et c'est une capacité que la
  salle rend possible et qu'un fichier ne rend pas : on observe pendant, pas
  après. Réserve à retenir pour la demande n° 2 : nous l'avons construit à la
  main, avec de la discipline et une surveillance mutuelle, là où une simple
  restriction d'écriture le rendrait sûr.

## Le solde, et de quoi il dépend

Sur cette seule séance, la salle a évité trois erreurs de documentation en les
corrigeant en un aller-retour, et elle a produit une fuite qui a invalidé un
run de validation complet.

Cette fuite ne vient pas de la fonction de la salle, elle vient du défaut
d'identité, déclenché en y faisant entrer un sous-agent. Le solde dépend donc
entièrement de la corrigibilité de ce défaut. Corrigé, une salle qui évite
trois erreurs de documentation par séance est clairement rentable. Non
corrigé, l'outil reste piégé pour tout usage impliquant des sous-agents,
c'est-à-dire l'usage naturel.

## Note sur la valeur, et ce qu'elle implique pour un lecteur

La valeur ne vient pas de la messagerie mais de **l'indépendance des
participants**, dont l'asymétrie et la corroboration sont deux exploitations
distinctes. L'asymétrie trouve ce qu'un seul ne verrait pas : chacun de nous a
trouvé ce que l'autre ne pouvait pas voir, et les agents en chambre froide ont
trouvé ce qu'aucun de nous deux ne pouvait voir, l'un d'eux découvrant même un
bug que le précédent ne pouvait pas trouver, uniquement parce que son bac à
sable avait d'autres droits. La corroboration confirme ce qu'un seul n'oserait
pas affirmer : nous sommes arrivés deux fois à la même conclusion par des
chemins séparés.

Ce que caucus apporte en propre, et qui justifie son existence, c'est la
**contradiction à faible latence entre pairs qui travaillent en même temps**.
Trois affirmations fausses ont été corrigées en un aller-retour avant de se
propager dans la documentation, dont une consigne qui était activement
dangereuse. Aucune de ces corrections n'aurait été rentable en passant par un
artefact : trop lente, trop lourde, et pour la dernière, appliquée avant
relecture.

**Cette valeur a une condition qu'il faut nommer : la simultanéité.** Les
trois corrections ont fonctionné parce que nous travaillions au même moment
sur le même objet. Entre pairs qui travaillent en alternance, la salle
dégénère en boîte aux lettres, ce que le protocole refuse d'être à juste
titre, et l'artefact durable redevient supérieur. C'est le critère pratique
qui dit quand utiliser caucus et quand il n'est qu'un coût.

D'où la mise en garde pour qui lirait ce bilan comme une recommandation
d'outillage. Nos défauts se répartissent en trois familles, et une seule
appelle caucus :

1. **Ce qui se règle par une notation, sans second agent** : marquer ce qui
   est observé et ce qui est inféré, à la source comme dans la documentation.
   La moitié de nos défauts de documentation relevaient de ce seul remède, et
   l'ont prouvé en disparaissant dès qu'il a été appliqué.
2. **Ce qui exige un participant réellement différent** : l'ignorance d'un
   testeur qui n'a jamais vu le sujet, et un environnement d'exécution
   dissemblable. Ces deux-là ont besoin d'isolement, ce qu'il ne faut pas
   confondre avec l'absence de caucus. Nos deux meilleures trouvailles
   viennent d'agents isolés par des moyens différents : le premier était
   entièrement hors de la salle, le second y était, membre d'un canal
   d'observatoire dont ses observateurs étaient exclus par leur seule
   discipline. C'est d'ailleurs le second qui a produit la meilleure
   trouvaille de la journée.
3. **Ce qui exige la salle** : la contradiction immédiate entre pairs
   simultanés.

C'est pour cette raison que le défaut d'identité des sous-agents est le plus
grave de la liste. Il ne dégrade pas la messagerie : il fusionne deux
participants censés être indépendants, et détruit donc la seule chose que
l'outil apporte en propre.

## Hiérarchie des demandes

1. Identité des sous-agents, et nettoyage des sessions orphelines.
2. Membre en lecture seule.
3. Suppression de la valeur par défaut de `say`.
4. Confort du watcher : persistance ou réveil groupé, marqueur de troncature,
   relecture des derniers messages, erreur de session explicite.
