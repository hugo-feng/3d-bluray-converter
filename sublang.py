"""字幕简繁识别模块：PGS 采样渲染 + OCR，用于区分多条「中文」字幕。

流程（全离线）：
  1. 从源 m2ts 一次性提取各中文字幕轨的样本 sup（前 N 秒，流复制，秒级）
  2. 提取每轨第一个「epoch start 且有对象」的显示集为独立小 sup
     （独立文件保证 ffmpeg 从头解析、状态完整）
  3. ffmpeg 合成到黑底并直接裁剪字幕区域（秒级）
  4. RapidOCR 识别 → 特征字统计 → 判定简体/繁体 + 样本文字

多轨并行；引擎单例懒加载。任何失败都安全返回（script=None）。
"""
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

SIMP_CHARS = set("""万与东个为义乐书买乱争云亚产亲亿从仓仪们价众优会伟传伤体余侠儿兰关兴军农冲决况冻净准减几凤凭凯击刘则刚创别卫却厂历厉压厌参双发变叠叶号叹后吓吕吗听启吴呐呒呓呕呖呗员呙呛呜咏咙咛咝咤响哑哒哓哔哕哗哙哜哝哟唛唠唡唢唤啧啬啭啮啰啴啸喷喽喾嗫嗳嘘嘤噜嚣团园囱围国图圆圣圹场坂坏块坚坛坝坞坟坠垄垅垆垒垩垫垭垯垱垲垴埘埙埚埯堑堕墙壮声壳壶处备复够头夹夺奋奖妇妈妩妪妫姗姜娄娅娆娇娈娱娲娴婳婴婵婶媪嫒嫔嫱嬷孙学孪宁宝实宠审宪宫宽宾寝对寻导寿将尔尘尝尧尴尸尽层屃屉届属屡屦屿岁岂岖岗岘岙岚岛岭岳岽岿峃峄峡峣峤峥峦崂崃崄崭嵘嵚嵛嵝巅巩巯币帅师帏帐帘帜带帧帮帱帻帼幂幞并广庄庆庐庑库应庙庞废廪开异弃张弥弯弹强归当录彦彻径徕忆忏忧忾怀态怂怃怄怅怆怜总怼怿恋恒恳恶恸恹恺恻恼恽悦悫悬悭悯惊惧惨惩惫惬惭惮惯愠愤愦愿慑懑懒懔戆戋戏戗战戬户扎扑执扩扪扫扬扰抚抛抟抠抡抢护报担拟拢拣拥拦拧拨择挂挚挛挜挝挞挟挠挡挢挣挤挥挦捞损捡换捣据掳掴掷掸掺掼揽揾揿搀搁搂搅携摄摅摆摇摈摊撄撑撵撷撸撺擞攒敌敛数斋斓斗斩断无旧时旷旸昙昼昽显晋晒晓晔晕晖暂暧术机杀杂权杆条来杨杩杰极构枞枢枣枥枧枨枪枫枭柜柠柽栀栅标栈栉栊栋栌栎栏树栖样栾桠桡桢档桤桥桦桧桨桩梦梼梾梿检棂椁椟椠椤椭楼榄榇榈榉槚槛槟槠横樯樱橥橱橹橼檩欢欤欧歼殁殇残殒殓殚殡殴毁毂毕毙毡气氢氩氲汇汉汤汹沟没沣沤沥沦沧沨沩沪泞泪泶泷泸泺泻泼泽泾洁洒洼浃浅浆浇浈浊测浍济浏浑浒浓浔浕涂涌涛涝涞涟涠涡涢涣涤润涧涨涩淀渊渌渍渎渐渑渔渖渗温湾湿溃溅溆滗滚滞滟滠满滢滤滥滦滨滩滪漤潆潇潋潍潜潴澜濑濒灏灭灯灵灾灿炀炉炖炜炝点炼炽烁烂烃烛烟烦烧烨烩烫烬热焕焖焘煴爱爷牍牦牵牺犊状犷犸犹狈狝狞独狭狮狯狰狱狲猃猎猕猡猪猫猬献獭玑玛玮环现玱玺珐珑珰珲琏琐琼瑶瑷璎瓒瓯电画畅畴疖疗疟疠疡疬疮疯疱疴痈痉痒痖痨痪痫瘅瘗瘘瘪瘫瘾瘿癞癣癫皑皱皲盏盐监盖盗盘眍眦眬着睁睐睑瞒瞩矫矶矾矿砀码砖砗砚砜砺砻砾础硁硕硖硗硙硚确硷碍碛碜碱礼祎祢祯祷祸禀禄禅离秃秆种积称秽秾稆税稣稳穑穷窃窍窎窑窜窝窥窦窭竖竞笃笋笔笕笺笼笾筑筚筛筜筝筹签简箓箦箧箨箩箪箫篑篓篮篱簖籁籴类籼粜粝粤粪粮糁糇紧絷纟红纣纤纥约级纨纩纪纫纬纭纯纰纱纲纳纴纵纶纷纸纹纺纻纼纽纾线绀绁绂练组绅细织终绉绊绋绌绍绎经绐绑绒结绔绕绖绗绘给绚绛络绝绞统绠绡绢绣绤绥绦继绨绩绪绫续绮绯绰绱绲绳维绵绶绷绸绹绺绻综绽绾绿缀缁缂缃缄缅缆缇缈缉缊缋缌缍缎缏缑缒缓缔缕编缗缘缙缚缛缜缝缟缠缡缢缣缤缥缦缧缨缩缪缫缬缭缮缯缰缱缲缳缴缵罂网罗罚罢罴羁羟羡翘耢耧耸耻聂聋职聍联聩聪肃肠肤肮肴肾肿胀胁胆胜胧胨胪胫胶脉脍脏脐脑脓脔脚脱脶脸腊腘腭腻腼腽腾膑臜舆舣舰舱舻艰艳艺节芈芗芜芦苁苇苈苋苌苍苎苏苘苹茎茏茑茔茕茧荆荐荙荚荛荜荞荟荠荡荣荤荥荦荧荨荩荪荫荬荭荮药莅莱莲莳莴莶获莸莹莺莼萚萝萤营萦萧萨葱蒇蒉蒋蒌蓝蓟蓠蓣蓥蓦蔷蔹蔺蕲蕴薮藓虏虑虚虫虬虮虽虾虿蚀蚁蚂蚕蚬蛊蛎蛏蛮蛰蛱蛲蛳蛴蜕蜗蜡蝇蝈蝉蝎蝼蝾螀螨蟏衅衔补衬衮袄袅袆袜袭袯装裆裈裢裣裤裥褛褴襁见观规觅视觇览觉觊觋觌觎觏觐觑觞触觯誉誊讠计订讣认讥讦讧讨让讪讫讬训议讯记讱讲讳讴讵讶讷许讹论讻讼讽设访诀证诂诃评诅识诇诈诉诊诋诌词诎诏诐译诒诓诔试诖诗诘诙诚诛诜话诞诟诠诡询诣诤该详诧诨诩诪诫诬语诮误诰诱诲诳说诵诶请诸诹诺读诼诽课诿谀谁谂调谄谅谆谇谈谊谋谌谍谎谏谐谑谒谓谔谕谖谗谘谙谚谛谜谝谟谠谡谢谣谤谥谦谧谨谩谪谫谬谭谮谯谰谱谲谳谴谵谶谷豮贝贞负贠贡财责贤败账货质贩贪贫贬购贮贯贰贱贲贳贴贵贶贷贸费贺贻贼贽贾贿赀赁赂赃资赅赆赇赈赉赊赋赌赍赎赏赐赑赒赓赔赕赖赗赘赙赚赛赜赝赞赟赠赡赢赣赪赵赶趋趱趸跃跄跞践跶跷跸跹跻踊踌踪踬踯蹑蹒蹰蹿躏躜躯车轧轨轩轪轫转轭轮软轰轱轲轳轴轵轶轷轸轹轺轻轼载轾轿辀辁辂较辄辅辆辇辈辉辊辋辌辍辎辏辐辑辒输辔辕辖辗辘辙辚辞辩辫边辽达迁过迈运还这进远违连迟迩迳迹适选逊递逦逻遗遥邓邝邬邮邹邺邻郁郏郐郑郓郦郧郸酝酦酱酽酾酿释里鉴銮錾钆钇针钉钊钋钌钍钎钏钐钑钒钓钔钕钖钗钘钙钚钛钜钝钞钟钠钡钢钣钤钥钦钧钨钩钪钫钬钭钮钯钰钱钲钳钴钵钶钷钸钹钺钻钼钽钾钿铀铁铂铃铄铅铆铈铉铊铋铌铍铎铏铐铑铒铓铔铕铖铗铘铙铚铛铜铝铞铟铠铡铢铣铤铥铦铧铨铩铪铫铬铭铮铯铰铱铲铳铴铵银铷铸铹铺铻铼铽链铿销锁锂锃锄锅锆锇锈锉锊锋锌锍锎锏锐锑锒锓锔锕锖锗锘错锚锛锜锝锞锟锠锡锢锣锤锥锦锧锨锩锪锫锬锭键锯锰锱锲锳锴锵锶锷锸锹锺锻锼锽锾锿镀镁镂镃镄镅镆镇镉镊镋镌镍镎镏镐镑镒镓镔镕镖镗镘镙镚镛镜镝镞镟镠镡镢镣镤镥镦镧镨镩镪镫镬镭镮镯镰镱镲镳镴镶长门闩闪闫闬闭问闯闰闱闲闳间闵闶闷闸闹闺闻闼闽闾闿阀阁阂阃阄阅阆阇阈阉阊阋阌阍阎阏阐阑阒阔阕阖阗阘阙阚队阳阴阵阶际陆陇陈陉陕陧陨险随隐隶隽难雏雠雳雾霁霉霭靓静靥鞑鞒鞯韦韧韨韩韪韫韬韵页顶顷顸项顺须顼顽顾顿颀颁颂颃预颅领颇颈颉颊颋颌颍颎颏颐频颒颓颔颕颖颗题颙颚颛颜额颞颟颠颡颢颣颤颥颦颧风飏飐飑飒飓飔飕飖飗飘飙飚飞飨餍饣饤饥饦饧饨饩饪饫饬饭饮饯饰饱饲饳饴饵饶饷饸饹饺饻饼饽饾饿馀馁馂馃馄馅馆馇馈馉馊馋馌馍馎馏馐馑馒馓馔馕马驭驮驯驰驱驲驳驴驵驶驷驸驹驺驻驼驽驾驿骀骁骂骃骄骅骆骇骈骉骊骋验骍骎骏骐骑骒骓骔骕骖骗骘骙骚骛骜骝骞骟骠骡骢骣骤骥骦骧髅髋髌鬓魇魉鱼鱽鱾鱿鲀鲁鲂鲃鲄鲅鲆鲇鲈鲉鲊鲋鲌鲍鲎鲏鲐鲑鲒鲓鲔鲕鲖鲗鲘鲙鲚鲛鲜鲝鲞鲟鲠鲡鲢鲣鲤鲥鲦鲧鲨鲩鲪鲫鲬鲭鲮鲯鲰鲱鲲鲳鲴鲵鲶鲷鲸鲹鲺鲻鲼鲽鲾鲿鳀鳁鳂鳃鳄鳅鳆鳇鳈鳉鳊鳋鳌鳍鳎鳏鳐鳑鳒鳓鳔鳕鳖鳗鳘鳙鳚鳛鳜鳝鳞鳟鳠鳡鳢鳣鳤鸟鸠鸡鸢鸣鸤鸥鸦鸧鸨鸩鸪鸫鸬鸭鸮鸯鸰鸱鸲鸳鸴鸵鸶鸷鸸鸹鸺鸻鸼鸽鸾鸿鹀鹁鹂鹃鹄鹅鹆鹇鹈鹉鹊鹋鹌鹍鹎鹏鹐鹑鹒鹓鹔鹕鹖鹗鹘鹙鹚鹛鹜鹝鹞鹟鹠鹡鹢鹣鹤鹥鹦鹧鹨鹩鹪鹫鹬鹭鹮鹯鹰鹱鹲鹳鹴鹾麦麸黄黉黡黩黪黾鼋鼍鼹齐齑齿龀龁龂龃龄龅龆龇龈龉龊龋龌龙龚龛龟""")
TRAD_CHARS = set("""䥑䰾䲁䲘䴉並亂亞來俠倉個們偉傑備傳傷價儀億優兒冪凍凱別則剛創劉勝匯卻厭厲參吒吳吶呂咼員唄問啞啟啢喚喲嗆嗇嗎嗚嗩嗶嘆嘍嘔嘖嘗嘜嘩嘮嘯嘵嘸嘽噓噝噠噥噦噯噲噴嚀嚇嚌嚕嚙嚦嚨嚳嚶囀囁囂囈囉囪國圍園圓圖團垵埡執堅堊堖堝堯報場塊塋塏塒塗塢塤塵塹墊墜墮墳墶壇壋壓壘壙壚壞壟壠壩壯壺壽夠夢夾奪奮姍娛婁婦婭媧媼媽嫗嫵嫻嫿嬀嬈嬋嬌嬙嬡嬤嬪嬰嬸孌孫學孿宮寢實寧審寬寵寶將尋對導尷屆屍屓屜屢層屨屬峴島峽崍崗崢崬崳嵐嶁嶄嶇嶔嶗嶠嶢嶧嶨嶮嶴嶸嶺嶼嶽巋巒巔巰帥師帳帶幀幃幗幘幟幣幫幬幾庫廟廠廡廢廣廩廬張強彈彌彎彠彥後徑從徠復徹恆恥悅悵悶惡惱惲惻愛愜愨愴愷愾態慍慘慚慟慣慪慫慮慳慶憂憊憐憑憒憚憤憫憮憲憶懇應懌懍懟懣懨懲懶懷懸懺懼懾戀戇戔戧戩戰戲戶拋挾捫掃掄掗掙掛揀揚換揮損搖搗搵搶摑摜摟摯摳摶摻撈撏撐撓撟撣撥撫撲撳撻撾撿擁擄擇擊擋擔據擠擬擯擰擱擲擴擷擺擻擼擾攄攆攏攔攖攙攛攜攝攢攣攤攪攬敗敵數斂斃斕斬斷時晉晝暈暉暘暢暫曄曇曉曖曠曨曬書會朧東柵桿梔梘條梟棄棖棗棟棧棲棶椏楊楓楨極榪榮榿構槍槤槧槨槳樁樂樅樓標樞樣樹樺橈橋機橢橫檁檉檔檜檟檢檣檮檳檸檻檾櫃櫓櫚櫛櫝櫞櫟櫥櫧櫨櫪櫫櫬櫳櫸櫻欄權欏欒欖欞欽歐歟歡歲歷歸歿殘殞殤殫殮殯殲殺殼毀毆氈氣氫氬氳決沒沖況洶浹涇淚淥淨淪淵淶淺渙減渢渦測渾湞湧湯準溝溫溳滄滅滌滎滬滯滲滸滾滿漁漚漢漣漬漲漵漸漿潁潑潔潙潛潤潯潰潷潿澀澆澇澗澠澤澦澩澮澱濁濃濕濘濜濟濤濫濰濱濺濼濾瀅瀆瀉瀋瀏瀕瀘瀝瀟瀠瀦瀧瀨瀲瀾灃灄灑灘灝灣灤灩災為烴無煉煒煙煢煥煩煬熅熒熗熱熲熾燁燈燉燒燙燜營燦燭燴燼燾爍爐爛爭爺爾牆牘牽犖犛犢犧狀狹狽猙猶猻獁獄獅獎獨獪獫獮獰獲獵獷獺獻獼玀現琺琿瑋瑣瑤瑩瑪瑲璉璣璦璫環璽瓊瓏瓔瓚甌產畢畫異當疇疊痙痾瘂瘋瘍瘓瘛瘞瘡瘧瘻療癆癇癉癘癟癢癤癧癩癬癭癮癰癱癲發皚皰皸皺盜盞盡監盤眥眾睜睞瞘瞞瞼矓矚矯硜硤硨硯碩碭碸確碼磑磚磣磧磯磽礄礎礙礦礪礫礬礱祿禍禎禕禪禮禰禱禿秈稅稈稟種稱穀穌積穎穠穡穢穩穭窩窪窮窯窵窶窺竄竅竇竊競筆筍筧箋箏節築篋篤篩篳簀簍簞簡簣簫簹簽簾籃籌籙籜籟籠籩籪籬籮粵糝糞糧糲糴糶糹紀紂約紅紇紈紉紋納紐紓純紕紖紗紙級紛紜紝紡紮細紱紲紳紵紹紺紼紿絀終組絆絎結絕絛絝絞絡絢給絨絰統絳絹綁綃綆綈綌綏經綜綞綠綢綣綬維綯綰綱網綴綸綹綺綻綽綾綿緄緇緊緋緒緔緗緘緙線緝緞締緡緣緦編緩緬緯緱緲練緶緹縈縉縊縋縐縑縕縛縝縞縟縫縭縮縱縲縵縶縷縹總績繃繅繆繈繒織繕繚繞繡繢繩繪繭繯繰繳繹繼繽繾纇纈纊續纏纓纖纘纜缽罌罰罵罷羅羆羈羋羥羨義翹耬耮聖聞聯聰聲聳聵聶職聹聽聾肅脅脈脛脫脹腎腖腡腦腫腳腸膃膕膚膠膩膽膾膿臉臍臏臘臚臟臠臢與興舊艙艤艦艫艱艷苧荊莊莖莢莧萇萊萬萵葉葒著葤葦葷蒔蒞蒼蓀蓋蓮蓯蓴蓽蔞蔣蔥蔦蔭蕁蕆蕎蕒蕕蕘蕢蕩蕪蕭蕷薈薊薌薑薔薘薟薦薩薺藍藎藝藥藪藶藺蘀蘄蘆蘇蘊蘋蘚蘞蘢蘭蘺蘿處虛虜號虯蛺蛻蜆蝕蝟蝦蝸螄螞螢螻螿蟄蟈蟎蟣蟬蟯蟲蟶蟻蠅蠆蠍蠐蠑蠟蠣蠨蠱蠶蠻術衛袞裊補裝裡褌褘褲褳褸襆襉襏襖襝襠襤襪襯襲見規覓視覘覡覦親覬覯覲覷覺覽覿觀觴觶觸訁訂訃計訊訌討訐訒訓訕訖託記訛訝訟訣訥訩訪設許訴訶診詁詆詎詐詒詔評詖詗詘詛詞詠詡詢詣試詩詫詬詭詮詰話該詳詵詼詿誄誅誆認誑誒誕誘誚語誠誡誣誤誥誦誨說誰課誶誹誼誾調諂諄談諉請諍諏諑諒論諗諛諜諞諢諤諦諧諫諭諮諱諳諶諷諸諺諼諾謀謁謂謄謅謊謎謐謔謖謗謙謚講謝謠謨謫謬謳謹謾證譎譏譖識譙譚譜譫譯議譴護譸譽譾讀變讋讎讒讓讕讖讜讞豈豎豬豶貓貝貞貟負財貢貧貨販貪貫責貯貰貲貳貴貶買貸貺費貼貽貿賀賁賂賃賄賅資賈賊賑賒賓賕賙賚賜賞賠賡賢賤賦賧質賬賭賴賵賺賻購賽賾贄贅贇贈贊贍贏贐贓贔贖贗贛赬趕趙趨趲跡踐踴蹌蹕蹣蹤蹺躂躉躊躋躍躑躒躓躕躚躡躥躦躪軀車軋軌軍軑軒軔軛軟軤軫軲軸軹軺軻軼軾較輅輇輈載輊輒輔輕輛輜輝輞輟輥輦輩輪輬輯輳輸輻輾輿轀轂轄轅轆轉轍轎轔轟轡轢轤辭辮辯農逕這連進運過達違遙遜遞遠適遲遷選遺遼邁還邇邊邏邐郟郵鄆鄒鄔鄖鄧鄭鄰鄲鄴鄶鄺酈醞醬醱釀釁釃釅釋釓釔釕釗釘釙針釣釤釧釩釵釷釹釺鈀鈁鈃鈄鈈鈉鈍鈐鈑鈒鈔鈕鈞鈣鈥鈦鈧鈮鈰鈳鈴鈷鈸鈹鈺鈽鈾鈿鉀鉅鉈鉉鉍鉑鉕鉗鉚鉛鉞鉤鉦鉬鉭鉶鉸鉺鉻鉿銀銃銅銍銑銓銖銘銚銛銜銠銣銥銦銨銩銪銫銬銱銳銷銻銼鋁鋃鋅鋇鋌鋏鋒鋙鋝鋟鋣鋤鋥鋦鋨鋩鋪鋮鋯鋰鋱鋶鋸鋼錁錄錆錇錈錏錐錒錕錘錙錚錛錟錠錡錢錦錨錩錫錮錯錳錸鍀鍁鍃鍆鍇鍈鍋鍍鍔鍘鍚鍛鍠鍤鍥鍩鍬鍰鍵鍶鍺鍾鎂鎄鎇鎊鎔鎖鎘鎡鎢鎣鎦鎧鎨鎩鎪鎬鎮鎰鎳鎵鎿鏃鏇鏈鏌鏍鏐鏑鏗鏘鏜鏝鏞鏟鏡鏢鏤鏨鏰鏵鏷鏹鏽鐃鐋鐐鐒鐓鐔鐘鐙鐛鐠鐦鐧鐨鐫鐮鐲鐳鐵鐶鐸鐺鐿鑄鑊鑌鑑鑔鑕鑞鑠鑣鑥鑭鑰鑲鑷鑹鑼鑽鑾钁钂長門閂閃閆閈閉開閌閎閏閒間閔閘閡閣閥閨閩閫閬閭閱閶閹閻閼閽閾閿闃闈闊闋闌闍闐闒闓闔闕闖關闞闡闥阪陘陝陣陰陳陸陽隉隊階隕際隨險隱隴隸雋雖雙雛雜雞離難雲電霧霽靂靄靈靚靜靦靨鞏鞽韁韃韉韋韌韍韓韙韜韞韻響頁頂頃項順頇須頊頌頎頏預頑頒頓頗領頜頡頤頦頭頮頰頲頴頷頸頹頻顆題額顎顏顒顓願顙顛類顢顥顧顫顬顯顰顱顳顴風颭颮颯颶颸颺颻颼飀飄飆飈飛飠飢飣飥飩飪飫飭飯飲飴飼飽飾飿餃餄餅餉餌餎餏餑餒餓餕餖餘餚餛餜餞餡館餱餳餶餷餺餼餾餿饁饃饅饈饉饊饋饌饒饗饜饞饢馬馭馱馳馴馹駁駐駑駒駔駕駘駙駛駝駟駢駭駰駱駸駿騁騂騅騌騍騎騏騖騙騤騫騭騮騰騶騷騸騾驀驁驂驃驄驅驊驌驍驏驕驗驚驛驟驢驤驥驦驪驫骯髏體髕髖鬢鬥鬧鬩鬮鬱魎魘魚魛魢魨魯魴魷魺鮁鮃鮊鮋鮍鮎鮐鮑鮒鮓鮚鮜鮞鮦鮪鮫鮭鮮鮳鮶鮺鯀鯁鯇鯉鯊鯒鯔鯕鯖鯗鯛鯝鯡鯢鯤鯧鯨鯪鯫鯰鯴鯵鯷鯽鯿鰁鰂鰃鰈鰉鰍鰏鰒鰓鰜鰟鰠鰣鰥鰨鰩鰭鰮鰱鰲鰳鰵鰷鰹鰻鰼鰾鱂鱅鱈鱉鱒鱔鱖鱗鱘鱝鱟鱠鱡鱣鱧鱨鱭鱯鱷鱸鱺鳥鳩鳲鳳鳴鳶鴆鴇鴉鴒鴕鴛鴝鴞鴟鴣鴦鴨鴯鴰鴴鴻鴿鵂鵃鵐鵑鵒鵓鵜鵝鵠鵪鵬鵮鵯鵲鵷鵾鶇鶉鶊鶓鶖鶘鶚鶡鶥鶩鶬鶯鶲鶴鶹鶺鶻鶼鶿鷁鷂鷊鷓鷖鷗鷙鷚鷥鷦鷫鷯鷲鷳鷸鷹鷺鷽鸇鸌鸏鸕鸘鸚鸛鸝鸞鹺鹼鹽麥麩黃黌點黲黴黶黷黽黿鼉鼴齊齋齎齏齒齔齕齗齙齜齟齠齡齦齪齬齲齶齷龍龐龔龕龜""")

_CREATE_NO_WINDOW = 0x08000000   # 防止子进程弹出黑色控制台窗口

_ENGINE = None
_ENGINE_PROV = ""
_LOCK = threading.Lock()
_OCR_RUN_LOCK = threading.Lock()   # DirectML 推理必须串行（并发会死锁）


def ocr_engine():
    global _ENGINE, _ENGINE_PROV
    if _ENGINE is None:
        with _LOCK:
            if _ENGINE is None:
                from rapidocr_onnxruntime import RapidOCR
                eng = None
                # 优先 DirectML（Windows 10+，支持任意 DX12 显卡：
                # NVIDIA 老卡如 GT 710/GT 1030、AMD、Intel 核显均可加速）
                for kw in ({"use_cls": False, "det_use_dml": True,
                           "rec_use_dml": True},
                           {"use_cls": False}):
                    try:
                        eng = RapidOCR(**kw)
                        break
                    except Exception:
                        eng = None
                if eng is None:
                    eng = RapidOCR(use_cls=False)
                try:
                    _ENGINE_PROV = eng.text_rec.session.session \
                        .get_providers()[0]
                except Exception:
                    _ENGINE_PROV = "CPUExecutionProvider"
                # 预热一次真实推理：把模型初始化/显存分配的开销
                # 提前到程序启动阶段（后台线程），首个真实请求即可全速
                try:
                    import numpy as _np
                    from PIL import Image as _Im
                    img = _Im.new("RGB", (96, 40), (0, 0, 0))
                    eng(_np.array(img))
                except Exception:
                    pass
                _ENGINE = eng
    return _ENGINE


def engine_provider():
    """返回 OCR 推理后端名（如 DmlExecutionProvider / CPUExecutionProvider）"""
    try:
        ocr_engine()
    except Exception:
        pass
    return _ENGINE_PROV or "CPUExecutionProvider"


def ocr_available():
    try:
        import rapidocr_onnxruntime  # noqa
        return True
    except Exception:
        return False


def detect_script(text):
    """返回 ('simplified'|'traditional'|None, 繁体命中数, 简体命中数)"""
    if not text:
        return None, 0, 0
    s = sum(1 for ch in text if ch in SIMP_CHARS)
    t = sum(1 for ch in text if ch in TRAD_CHARS)
    if t > s:
        return "traditional", t, s
    if s > t:
        return "simplified", t, s
    return None, t, s


def _read_segs(path, stop_after_first_epoch=False):
    data = open(path, "rb").read()
    segs = []
    i = 0
    n = len(data)
    got_epoch = False
    while i + 13 <= n:
        if data[i:i + 2] != b"PG":
            j = data.find(b"PG", i + 1)
            if j < 0:
                break
            i = j
            continue
        pts = int.from_bytes(data[i + 2:i + 6], "big") / 90000.0
        typ = data[i + 10]
        size = int.from_bytes(data[i + 11:i + 13], "big")
        payload = data[i + 13:i + 13 + size]
        segs.append((pts, typ, payload))
        if stop_after_first_epoch:
            if typ == 0x16 and len(payload) >= 11 \
                    and ((payload[7] >> 6) & 3) == 2 and payload[10] > 0:
                got_epoch = True
            elif got_epoch and typ == 0x80:  # 该显示集结束
                break
        i += 13 + size
    return segs


def _extract_epochs(sup, prefix, max_n=2):
    """提取前 N 个「epoch start 且有对象」显示集为独立小 sup。
    返回 [(small_path, boxes, canvas, total_w)]"""
    try:
        segs = _read_segs(sup, stop_after_first_epoch=(max_n <= 1))
    except Exception:
        return []
    res = []
    k = 0
    while k < len(segs) and len(res) < max_n:
        _pts, typ, p = segs[k]
        if typ == 0x16 and len(p) >= 11 and ((p[7] >> 6) & 3) == 2 \
                and p[10] > 0:
            canvas = int.from_bytes(p[0:2], "big") or 1920
            objs = []
            o = 11
            for _ in range(p[10]):
                if o + 8 > len(p):
                    break
                oid = int.from_bytes(p[o:o + 2], "big")
                x = int.from_bytes(p[o + 4:o + 6], "big")
                y = int.from_bytes(p[o + 6:o + 8], "big")
                objs.append((oid, x, y))
                o += 8
            end = k + 1
            while end < len(segs) and segs[end][1] != 0x16:
                end += 1
            batch = segs[k:end]
            dims = {}
            for (_p2, t2, pp) in batch:
                if t2 == 0x15 and len(pp) >= 11 and (pp[3] & 0x80):
                    oid = int.from_bytes(pp[0:2], "big")
                    dims[oid] = (int.from_bytes(pp[7:9], "big"),
                                 int.from_bytes(pp[9:11], "big"))
            boxes = [(x, y, *dims.get(oid, (0, 0))) for (oid, x, y) in objs]
            boxes = [b for b in boxes if b[2] > 0]
            if boxes:
                t0 = batch[0][0]
                out = bytearray()
                for (pts2, t2, pp) in batch:
                    np_ = max(0, int(round((pts2 - t0) * 90000)))
                    out += b"PG" + np_.to_bytes(4, "big") \
                        + np_.to_bytes(4, "big") + bytes([t2]) \
                        + len(pp).to_bytes(2, "big") + pp
                sp = "%s_%d.sup" % (prefix, len(res))
                with open(sp, "wb") as f:
                    f.write(bytes(out))
                res.append((sp, boxes, canvas,
                            sum(b[2] for b in boxes)))
            k = end
        else:
            k += 1
    return res


def _render_crop(ffmpeg, small, canvas, boxes, png):
    xs = [b[0] for b in boxes]
    ys = [b[1] for b in boxes]
    x2 = [b[0] + b[2] for b in boxes]
    y2 = [b[1] + b[3] for b in boxes]
    x0 = max(0, min(xs) - 8)
    y0 = max(0, min(ys) - 8)
    w = min(canvas, max(x2)) - x0 + 16
    h = max(y2) - y0 + 16
    cmd = [ffmpeg, "-hide_banner", "-y", "-nostats",
           "-f", "lavfi",
           "-i", "color=c=black:s=%dx1080:d=1:r=24" % canvas,
           "-i", small,
           "-filter_complex",
           "[0:v][1:s]overlay=0:0,crop=%d:%d:%d:%d" % (w, h, x0, y0),
           "-frames:v", "2", png]
    subprocess.run(cmd, capture_output=True, timeout=90,
                   creationflags=_CREATE_NO_WINDOW)


def _ocr_png(png):
    from PIL import Image
    eng = ocr_engine()
    im = Image.open(png).convert("RGB")
    if im.width > 1100:
        im = im.resize((im.width // 2, im.height // 2), Image.LANCZOS)
    # DirectML 推理需串行执行：多线程并发 session.run 会死锁
    with _OCR_RUN_LOCK:
        res, _ = eng(im)
    texts = []
    for r in (res or []):
        if r[2] > 0.5 and r[1] not in texts:
            texts.append(r[1])
    return "".join(texts)


def _analyze_track(ffmpeg, idx, sup, workdir, log=None):
    def _lg(s):
        if log:
            try:
                log(s)
            except Exception:
                pass
    try:
        t0 = time.time()
        kb = os.path.getsize(sup) / 1024.0
        _lg("  [轨%d] 样本 %.0f KB，开始解析显示集..." % (idx, kb))
        epochs = _extract_epochs(sup, os.path.join(workdir, "sb%d" % idx),
                                 max_n=1)
        if not epochs:
            _lg("  [轨%d] 未找到可用显示集（片头可能无字幕），跳过" % idx)
            return idx, {"script": None, "text": ""}
        small, boxes, canvas, _tw = epochs[0]
        _lg("  [轨%d] 显示集解析完成（画布 %d，对象 %d 个），渲染字幕帧..."
            % (idx, canvas, len(boxes)))
        t_r = time.time()
        png = os.path.join(workdir, "sbr%d_%%d.png" % idx)
        _render_crop(ffmpeg, small, canvas, boxes, png)
        frames = sorted(f for f in os.listdir(workdir)
                        if f.startswith("sbr%d_" % idx))
        if not frames:
            _lg("  [轨%d] 渲染失败，跳过" % idx)
            return idx, {"script": None, "text": ""}
        _lg("  [轨%d] 渲染完成（%.2fs），OCR 识别中..."
            % (idx, time.time() - t_r))
        t_o = time.time()
        text = _ocr_png(os.path.join(workdir, frames[-1]))
        _lg("  [轨%d] OCR 完成（%.2fs）：\u300c%s\u300d"
            % (idx, time.time() - t_o, text or "（无文本）"))
        script, tt, ss = detect_script(text)
        name = {"simplified": "简体", "traditional": "繁体"}.get(
            script, "无法判定")
        _lg("  [轨%d] 判定：%s（繁体特征字 %d / 简体特征字 %d，总耗时 %.2fs）"
            % (idx, name, tt, ss, time.time() - t0))
        return idx, {"script": script, "text": text}
    except Exception as e:
        _lg("  [轨%d] 识别异常：%r" % (idx, e))
        return idx, {"script": None, "text": ""}


def detect_tracks_scripts(m2ts, positions, ffmpeg, workdir,
                          max_seconds=240, timeout=180, on_log=None):
    """识别指定字幕轨（ffmpeg 字幕流序号）的简繁。
    返回 {pos: {"script": 'simplified'/'traditional'/None, "text": 样本}}
    on_log: 可选日志回调（进度/结果会实时上报，写入界面与运行日志）。
    """
    def _lg(s):
        if on_log:
            try:
                on_log(s)
            except Exception:
                pass
    out = {}
    positions = [int(p) for p in positions]
    if not positions:
        return out
    if not ocr_available():
        _lg("字幕语言识别：OCR 组件不可用，已跳过（列表保持原样）")
        for p in positions:
            out[p] = {"script": None, "text": ""}
        return out
    _lg("字幕语言识别：开始（%d 条中文字幕，采样前 %d 秒）"
        % (len(positions), max_seconds))
    t_all = time.time()
    try:
        os.makedirs(workdir, exist_ok=True)
    except Exception:
        return {p: {"script": None, "text": ""} for p in positions}
    sups = {}
    try:
        cmd = [ffmpeg, "-hide_banner", "-y", "-nostats", "-i", m2ts]
        for p in positions:
            sp = os.path.join(workdir, "sbs_%d.sup" % p)
            cmd += ["-map", "0:s:%d" % p, "-c", "copy",
                    "-t", str(max_seconds), sp]
            sups[p] = sp
        subprocess.run(cmd, capture_output=True, timeout=timeout,
                       creationflags=_CREATE_NO_WINDOW)
        sups = {p: s for p, s in sups.items()
                if os.path.exists(s) and os.path.getsize(s) > 1024}
    except Exception as e:
        _lg("字幕语言识别：样本提取失败（%r），已跳过" % e)
        return {p: {"script": None, "text": ""} for p in positions}
    if not sups:
        _lg("字幕语言识别：样本提取为空（片头无字幕），已跳过")
        return {p: {"script": None, "text": ""} for p in positions}
    _lg("字幕语言识别：样本提取完成（%.2fs），OCR 引擎准备中..."
        % (time.time() - t_all))
    try:
        ocr_engine()
    except Exception:
        pass
    _lg("字幕语言识别：OCR 推理后端 = %s" % engine_provider())
    # 并行识别（各轨独立：解析 → 渲染 → OCR；渲染为进程级、OCR 有内在并行度，
    # 实测并行 3 轨明显快于串行）
    try:
        with ThreadPoolExecutor(max_workers=min(4, len(sups))) as ex:
            futs = [ex.submit(_analyze_track, ffmpeg, p, s, workdir,
                              on_log) for p, s in sups.items()]
            for f in futs:
                idx, r = f.result()
                out[idx] = r
    except Exception:
        pass
    for p in positions:
        out.setdefault(p, {"script": None, "text": ""})
    _lg("字幕语言识别：全部完成（总耗时 %.2fs）" % (time.time() - t_all))
    return out
