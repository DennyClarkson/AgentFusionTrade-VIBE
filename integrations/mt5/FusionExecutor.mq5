#property copyright "AgentTradeFusion"
#property version "3.00"
#property description "Native demo execution. AI manages parameters and bounded entry permission."
#property strict

input string MonitorSymbol="XAUUSD";
string active_symbol,folder="AgentTradeFusion\\";
#include "FusionKernel.mqh"

struct Packet { string keys[]; string values[]; };
Packet config;
long pinned_login=0,config_revision=0,command_revision=0,lease_until=0;
string pinned_server,instance_id,owner_session="",state="paused",reason="awaiting configuration",protection="broker SL/TP",pending_comment="";
int owner_file=INVALID_HANDLE,last_retcode=0,pending_kind=0;
ulong pending_ticket=0;
double pending_sl=0,pending_tp=0;
datetime pending_time=0,last_bar=0,last_attempt=0,calendar_query=0,last_control_issued=0;
datetime calendar_attempt=0,planned_h1=0,planned_d1=0;
long last_close_revision=0;
bool entry_permission=false,calendar_ok=false,configured=false,inside_tick=false;
double permission_scale=0;
datetime calendar_events[];
bool calendar_tentative=false;
FusionFrame current_minute;

string Get(Packet &p,const string key)
{
   for(int i=0;i<ArraySize(p.keys);i++) if(p.keys[i]==key) return p.values[i];
   return "";
}
bool Load(const string name,Packet &p)
{
   ArrayResize(p.keys,0);ArrayResize(p.values,0);
   int f=FileOpen(folder+name,FILE_READ|FILE_CSV|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ,';',CP_UTF8);
   if(f==INVALID_HANDLE) return false;
   bool complete=false;
   while(!FileIsEnding(f) && ArraySize(p.keys)<4096)
   {
      string key=FileReadString(f),value=FileReadString(f);
      if(key=="END") {complete=(int)StringToInteger(value)==ArraySize(p.keys);break;}
      if(key=="" || Get(p,key)!="") break;
      int n=ArraySize(p.keys);ArrayResize(p.keys,n+1);ArrayResize(p.values,n+1);
      p.keys[n]=key;p.values[n]=value;
   }
   FileClose(f);return complete;
}
void Put(Packet &p,const string key,const string value)
{
   int n=ArraySize(p.keys);ArrayResize(p.keys,n+1);ArrayResize(p.values,n+1);p.keys[n]=key;p.values[n]=value;
}
bool SavePacket(const string name,Packet &p)
{
   string temp=name+".tmp";ResetLastError();
   int f=FileOpen(folder+temp,FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON,';',CP_UTF8);
   if(f==INVALID_HANDLE) return false;
   bool ok=true;
   for(int i=0;i<ArraySize(p.keys);i++) if(FileWrite(f,p.keys[i],p.values[i])==0) ok=false;
   if(FileWrite(f,"END",ArraySize(p.keys))==0) ok=false;
   FileFlush(f);if(GetLastError()!=0) ok=false;FileClose(f);
   if(!ok || !Publish(temp,name)) return false;
   Packet verified;
   if(!Load(name,verified) || ArraySize(verified.keys)!=ArraySize(p.keys)) return false;
   for(int i=0;i<ArraySize(p.keys);i++) if(Get(verified,p.keys[i])!=p.values[i]) return false;
   return true;
}
double Number(Packet &p,const string key,const double lo,const double hi,bool &ok)
{
   string raw=Get(p,key);int digits=0,dots=0;
   for(int i=0;i<StringLen(raw);i++)
   {
      ushort c=StringGetCharacter(raw,i);
      if(c>='0' && c<='9') digits++;
      else if(c=='.') dots++;
      else if(c!='-' || i!=0) ok=false;
   }
   double value=StringToDouble(raw);
   if(digits==0 || dots>1 || !MathIsValidNumber(value) || value<lo || value>hi) ok=false;
   return value;
}
double N(const string key) {return StringToDouble(Get(config,key));}
long Magic() {return (long)N("magic");}
bool Identity()
{
   return AccountInfoInteger(ACCOUNT_LOGIN)==pinned_login && AccountInfoString(ACCOUNT_SERVER)==pinned_server && AccountInfoInteger(ACCOUNT_TRADE_MODE)==ACCOUNT_TRADE_MODE_DEMO && AccountInfoString(ACCOUNT_CURRENCY)=="USD";
}
bool TradingAllowed()
{
   return Identity() && TerminalInfoInteger(TERMINAL_CONNECTED) && TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) && MQLInfoInteger(MQL_TRADE_ALLOWED) && AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) && AccountInfoInteger(ACCOUNT_TRADE_EXPERT);
}
bool Publish(const string source,const string target) {return FileMove(folder+source,FILE_COMMON,folder+target,FILE_COMMON|FILE_REWRITE);}
string Clean(string value) {StringReplace(value,";",",");StringReplace(value,"\r"," ");StringReplace(value,"\n"," ");return value;}
string AccountFile(const string suffix) {return "native_"+(string)pinned_login+"_"+(string)Magic()+"_"+suffix;}

bool ValidateConfig(Packet &p)
{
   bool ok=true;
   if(Get(p,"protocol")!="FUSION_EXEC_CONFIG1" || (long)StringToInteger(Get(p,"login"))!=pinned_login || Get(p,"server")!=pinned_server || Get(p,"symbol")!=MonitorSymbol) return false;
   Number(p,"revision",1,4503599627370495.0,ok);Number(p,"magic",1,2147483647,ok);
   double fast=Number(p,"fast_ema",2,200,ok),slow=Number(p,"slow_ema",3,400,ok);
   if(fast>=slow) ok=false;
   Number(p,"atr_period",3,100,ok);Number(p,"bollinger_period",10,60,ok);Number(p,"bollinger_deviation",1,3.5,ok);
   Number(p,"min_trend_atr",0,5,ok);Number(p,"trend_adx",10,40,ok);Number(p,"trend_slope_atr",0,.5,ok);
   string bias=Get(p,"range_bias");if(bias!="H1" && bias!="D1" && bias!="consensus") ok=false;
   Number(p,"stop_atr",.5,10,ok);Number(p,"reward_risk",1,10,ok);Number(p,"trailing_start_r",.5,3,ok);
   Number(p,"trailing_atr",.3,3,ok);Number(p,"breakeven_r",.3,3,ok);Number(p,"max_hold_minutes",5,1440,ok);
   Number(p,"capital",.01,1e8,ok);Number(p,"risk_per_trade_pct",.001,5,ok);Number(p,"daily_loss_pct",.001,20,ok);
   Number(p,"max_positions",1,10,ok);Number(p,"max_spread_points",.01,100000,ok);Number(p,"max_tick_age_seconds",2,300,ok);
   Number(p,"max_slippage_points",0,1000,ok);Number(p,"max_margin_pct",.01,80,ok);Number(p,"commission_per_lot",0,1000,ok);
   double reduce=Number(p,"volatility_reduce_ratio",1,10,ok),pause=Number(p,"volatility_pause_ratio",1.1,20,ok);if(pause<=reduce) ok=false;
   for(int h=0;h<24;h++)
   {
      Number(p,"hour_"+(string)h+"_risk_scale",0,1,ok);Number(p,"hour_"+(string)h+"_stop_scale",.5,3,ok);Number(p,"hour_"+(string)h+"_target_scale",.5,3,ok);
   }
   Number(p,"session_start_utc",0,23,ok);Number(p,"session_end_utc",0,23,ok);Number(p,"block_weekends",0,1,ok);Number(p,"require_calendar",0,1,ok);
   Number(p,"blackout_before_minutes",0,240,ok);Number(p,"blackout_after_minutes",0,240,ok);
   Number(p,"caution_before_minutes",0,1440,ok);Number(p,"caution_after_minutes",0,1440,ok);
   Number(p,"event_risk_scale",0,1,ok);Number(p,"event_stop_scale",.5,3,ok);Number(p,"event_target_scale",.5,3,ok);Number(p,"close_before_event_minutes",0,240,ok);
   int count=(int)Number(p,"event_count",0,500,ok);
   for(int i=0;i<count;i++)
   {
      string k="event_"+(string)i;
      Number(p,k+"_time",1,10000000000.0,ok);Number(p,k+"_blackout_before_minutes",0,240,ok);Number(p,k+"_blackout_after_minutes",0,240,ok);
   }
   return ok;
}
void ApplyConfig(Packet &p)
{
   config=p;config_revision=(long)StringToInteger(Get(config,"revision"));strategy_revision=config_revision;
   active_symbol=Get(config,"symbol");ea_fast=(int)N("fast_ema");ea_slow=(int)N("slow_ema");ea_atr=(int)N("atr_period");ea_bb=(int)N("bollinger_period");
   ea_deviation=N("bollinger_deviation");ea_separation=N("min_trend_atr");ea_adx=N("trend_adx");ea_slope=N("trend_slope_atr");ea_bias=Get(config,"range_bias");
   ea_stop=N("stop_atr");ea_reward=N("reward_risk");ea_trail_start=N("trailing_start_r");ea_trail=N("trailing_atr");ea_breakeven=N("breakeven_r");ea_hold=(int)N("max_hold_minutes");
   configured=true;last_bar=0;
}
void ReadConfig()
{
   Packet p;
   if(!Load("execution.csv",p)) return;
   if((long)StringToInteger(Get(p,"revision"))==config_revision) return;
   if(entry_permission || PositionsTotal()>0 || OrdersTotal()>0 || pending_kind>0) {reason="parameter update waits for paused flat account";return;}
   if(!ValidateConfig(p)) {reason="configuration rejected";return;}
   // Save the complete accepted envelope for protection after process/terminal restart.
   if(!SavePacket("executor.active.csv",p)) {reason="configuration persistence failed";return;}
   bool reload_state=!configured || Magic()!=(long)StringToInteger(Get(p,"magic"));
   ApplyConfig(p);if(reload_state) {last_attempt=0;pending_kind=0;LoadState();}Print("FusionExecutor configuration acknowledged: ",config_revision);
}

bool SaveState()
{
   Packet p;
   Put(p,"server",pinned_server);Put(p,"last_attempt",(string)(long)last_attempt);Put(p,"pending_kind",(string)pending_kind);
   Put(p,"pending_ticket",(string)pending_ticket);Put(p,"pending_sl",DoubleToString(pending_sl,8));Put(p,"pending_tp",DoubleToString(pending_tp,8));
   Put(p,"pending_time",(string)(long)pending_time);Put(p,"pending_comment",pending_comment);
   return SavePacket(AccountFile("state.csv"),p);
}
void LoadState()
{
   Packet p;
   string file=AccountFile("state.csv");
   if(!FileIsExist(folder+file,FILE_COMMON))
   {
      if(FileIsExist(folder+AccountFile("journal.csv"),FILE_COMMON) || OwnedPositions()>0) {pending_kind=9;reason="execution state missing; reconciliation required";}
      return;
   }
   if(!Load(file,p) || Get(p,"server")!=pinned_server) {pending_kind=9;reason="execution journal unreadable; reconciliation required";return;}
   bool valid=ArraySize(p.keys)==8;
   double kind=Number(p,"pending_kind",0,9,valid);
   Number(p,"last_attempt",0,10000000000.0,valid);Number(p,"pending_ticket",0,9e18,valid);Number(p,"pending_sl",0,1e12,valid);Number(p,"pending_tp",0,1e12,valid);Number(p,"pending_time",0,10000000000.0,valid);
   bool has_comment=false;for(int i=0;i<ArraySize(p.keys);i++) if(p.keys[i]=="pending_comment") has_comment=true;
   if(!valid || !has_comment || (kind!=0 && kind!=1 && kind!=2 && kind!=3 && kind!=9) || (kind>0 && kind<9 && (StringToInteger(Get(p,"pending_time"))<=0 || (kind>1 && StringToInteger(Get(p,"pending_ticket"))<=0) || (kind==1 && Get(p,"pending_comment")==""))))
   {pending_kind=9;reason="execution journal schema invalid; reconciliation required";return;}
   last_attempt=(datetime)StringToInteger(Get(p,"last_attempt"));pending_kind=(int)StringToInteger(Get(p,"pending_kind"));
   pending_ticket=(ulong)StringToInteger(Get(p,"pending_ticket"));pending_sl=StringToDouble(Get(p,"pending_sl"));pending_tp=StringToDouble(Get(p,"pending_tp"));
   pending_time=(datetime)StringToInteger(Get(p,"pending_time"));pending_comment=Get(p,"pending_comment");
}
void Journal(const string event,MqlTradeRequest &request,MqlTradeResult &result)
{
   int f=FileOpen(folder+AccountFile("journal.csv"),FILE_READ|FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ,';',CP_UTF8);
   if(f==INVALID_HANDLE) return;
   FileSeek(f,0,SEEK_END);
   FileWrite(f,(long)TimeGMT(),event,instance_id,config_revision,request.action,request.position,request.type,request.volume,request.price,request.sl,request.tp,request.comment,result.retcode,result.order,result.deal);
   FileFlush(f);FileClose(f);
}
int OwnedPositions()
{
   if(!configured || Magic()<=0) return 0;
   int n=0;for(int i=0;i<PositionsTotal();i++) if(PositionGetTicket(i)>0 && PositionGetInteger(POSITION_MAGIC)==Magic() && PositionGetString(POSITION_SYMBOL)==active_symbol) n++;
   return n;
}
bool Owned(const ulong ticket) {return configured && Magic()>0 && PositionSelectByTicket(ticket) && PositionGetInteger(POSITION_MAGIC)==Magic() && PositionGetString(POSITION_SYMBOL)==active_symbol;}
void Reconcile()
{
   if(pending_kind==0 || pending_kind==9 || !Identity() || !TerminalInfoInteger(TERMINAL_CONNECTED)) return;
   bool proven=false;
   if(pending_kind==1)
   {
      for(int i=0;i<PositionsTotal();i++)
         if(PositionGetTicket(i)>0 && PositionGetInteger(POSITION_MAGIC)==Magic() && PositionGetString(POSITION_SYMBOL)==active_symbol && PositionGetString(POSITION_COMMENT)==pending_comment) proven=true;
      if(HistorySelect(pending_time-60,TimeTradeServer()+60))
         for(int i=0;i<HistoryDealsTotal();i++) {ulong d=HistoryDealGetTicket(i);if(HistoryDealGetInteger(d,DEAL_MAGIC)==Magic() && HistoryDealGetString(d,DEAL_COMMENT)==pending_comment) proven=true;}
      // An outstanding broker order can still produce another fill; keep locked.
      for(int i=0;i<OrdersTotal();i++) if(OrderGetTicket(i)>0 && OrderGetInteger(ORDER_MAGIC)==Magic()) return;
   }
   if(pending_kind==2 && !PositionSelectByTicket(pending_ticket) && HistorySelect(pending_time-60,TimeTradeServer()+60))
      for(int i=0;i<HistoryDealsTotal();i++)
      {
         ulong d=HistoryDealGetTicket(i);
         if((ulong)HistoryDealGetInteger(d,DEAL_POSITION_ID)==pending_ticket && HistoryDealGetInteger(d,DEAL_MAGIC)==Magic() && HistoryDealGetInteger(d,DEAL_ENTRY)==DEAL_ENTRY_OUT) proven=true;
      }
   if(pending_kind==3 && Owned(pending_ticket))
   {
      double sl=PositionGetDouble(POSITION_SL),tp=PositionGetDouble(POSITION_TP),step=SymbolInfoDouble(active_symbol,SYMBOL_TRADE_TICK_SIZE);
      int sign=PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY ? 1 : -1;
      proven=sl>0 && (sl-pending_sl)*sign>=-step*.1 && MathAbs(tp-pending_tp)<step*.1;
   }
   if(pending_kind==3 && !PositionSelectByTicket(pending_ticket) && HistorySelect(pending_time-60,TimeTradeServer()+60))
      for(int i=0;i<HistoryDealsTotal();i++)
      {
         ulong d=HistoryDealGetTicket(i);
         if((ulong)HistoryDealGetInteger(d,DEAL_POSITION_ID)==pending_ticket && HistoryDealGetInteger(d,DEAL_MAGIC)==Magic() && HistoryDealGetInteger(d,DEAL_ENTRY)==DEAL_ENTRY_OUT) proven=true;
      }
   if(proven) {int old=pending_kind;pending_kind=0;if(!SaveState()) pending_kind=old;}
}

bool TickFresh(MqlTick &tick)
{
   if(!SymbolInfoTick(active_symbol,tick) || tick.bid<=0 || tick.ask<tick.bid) return false;
   long age=(long)(TimeTradeServer()-tick.time);
   return age>=-2 && age<=(long)N("max_tick_age_seconds");
}
void ReadControl(const bool in_flight=false)
{
   entry_permission=false;permission_scale=0;
   Packet p;
   if(!Load("control.csv",p)) {reason="manager disconnected; entries paused";return;}
   long now=(long)TimeGMT(),issued=(long)StringToInteger(Get(p,"issued_at")),until=(long)StringToInteger(Get(p,"valid_until"));
   long revision=(long)StringToInteger(Get(p,"revision"));
   if(Get(p,"protocol")!="FUSION_EXEC_CONTROL1" || Get(p,"instance_id")!=instance_id || (long)StringToInteger(Get(p,"login"))!=pinned_login || Get(p,"server")!=pinned_server || Get(p,"symbol")!=active_symbol || revision<command_revision) {reason="awaiting permission for this EA boot";return;}
   if(issued>now+2 || until<now || until-issued<1 || until-issued>120) {reason="entry lease expired; protection continues";return;}
   bool valid=true;double scale=Number(p,"risk_scale",0,1,valid);
   Number(p,"revision",1,9007199254740991.0,valid);Number(p,"issued_at",1,10000000000.0,valid);Number(p,"valid_until",1,10000000000.0,valid);
   double close_value=Number(p,"close_ticket",0,9e18,valid);
   string enabled=Get(p,"enabled");
   bool same_config=configured && (long)StringToInteger(Get(p,"config_revision"))==config_revision;
   if(!valid || (enabled!="0" && enabled!="1") || Get(p,"session")=="" || StringLen(Get(p,"session"))>64 || (enabled=="1" && !same_config) || (close_value>0 && (!same_config || enabled!="0"))) {reason="malformed/mismatched command rejected";return;}
   if(owner_session!="" && owner_session!=Get(p,"session") && Get(p,"enabled")=="1") {reason="new manager must pause before enabling";return;}
   owner_session=Get(p,"session");command_revision=revision;lease_until=until;last_control_issued=(datetime)issued;
   entry_permission=valid && Get(p,"enabled")=="1" && (long)StringToInteger(Get(p,"config_revision"))==config_revision && configured && (pending_kind==0 || in_flight);
   permission_scale=entry_permission ? scale : 0;
   if(!entry_permission) reason="manager paused entries; protection continues";
   ulong close_ticket=(ulong)StringToInteger(Get(p,"close_ticket"));
   if(!in_flight && close_ticket>0 && revision>last_close_revision && pending_kind==0)
   {
      last_close_revision=revision;
      ClosePosition(close_ticket);
   }
}

bool SessionOpen()
{
   MqlDateTime t;TimeToStruct(TimeGMT(),t);int a=(int)N("session_start_utc"),b=(int)N("session_end_utc");
   return !(N("block_weekends")>0 && (t.day_of_week==0 || t.day_of_week==6)) && (a==b || (a<b ? t.hour>=a && t.hour<b : t.hour>=a || t.hour<b));
}
void UpdateCalendar()
{
   if(calendar_attempt>0 && TimeGMT()-calendar_attempt<60) return;
   calendar_attempt=TimeGMT();
   MqlCalendarValue rows[];datetime server_now=TimeTradeServer();
   int count=CalendarValueHistory(rows,server_now-86400,server_now+86400,NULL,"USD");
   if(count<0) {calendar_ok=false;return;}
   datetime events[];bool tentative=false;bool complete=true;
   long offset=(long)MathRound((double)(server_now-TimeGMT())/900.0)*900;
   for(int i=0;i<count;i++)
   {
      MqlCalendarEvent e;
      if(!CalendarEventById(rows[i].event_id,e)) {complete=false;continue;}
      if(e.importance!=CALENDAR_IMPORTANCE_HIGH) continue;
      if(e.time_mode!=CALENDAR_TIMEMODE_DATETIME) tentative=true;
      int n=ArraySize(events);ArrayResize(events,n+1);events[n]=(datetime)(rows[i].time-offset);
   }
   if(!complete) {calendar_ok=false;return;}
   ArrayCopy(calendar_events,events);calendar_tentative=tentative;calendar_query=TimeGMT();calendar_ok=true;
}
void EventEffect(const datetime when,const double before,const double after,double &scale,double &stop,double &target,bool &close_existing)
{
   double delta=(double)(TimeGMT()-when)/60;
   if(delta>=-MathMax(before,N("caution_before_minutes")) && delta<=MathMax(after,N("caution_after_minutes")))
   {
      scale=MathMin(scale,delta>=-before && delta<=after ? 0 : N("event_risk_scale"));
      stop=MathMax(stop,N("event_stop_scale"));target=MathMin(target,N("event_target_scale"));
   }
   if(N("close_before_event_minutes")>0 && delta>=-N("close_before_event_minutes") && delta<=after) close_existing=true;
}
void EventPolicy(double &scale,double &stop,double &target,bool &close_existing)
{
   scale=1;stop=1;target=1;close_existing=false;
   for(int i=0;i<ArraySize(calendar_events);i++) EventEffect(calendar_events[i],N("blackout_before_minutes"),N("blackout_after_minutes"),scale,stop,target,close_existing);
   for(int i=0;i<(int)N("event_count");i++)
   {
      string k="event_"+(string)i;
      EventEffect((datetime)N(k+"_time"),N(k+"_blackout_before_minutes"),N(k+"_blackout_after_minutes"),scale,stop,target,close_existing);
   }
}
double DailyRemaining()
{
   datetime now=TimeGMT(),midnight=(datetime)((long)now/86400*86400);
   long offset=(long)MathRound((double)(TimeTradeServer()-now)/900.0)*900;
   if(!HistorySelect((datetime)(midnight+offset),TimeTradeServer()+60)) return 0;
   double realized=0,floating=0;
   for(int i=0;i<HistoryDealsTotal();i++)
   {
      ulong d=HistoryDealGetTicket(i);long type=HistoryDealGetInteger(d,DEAL_TYPE);
      if(type==DEAL_TYPE_BUY || type==DEAL_TYPE_SELL) realized+=HistoryDealGetDouble(d,DEAL_PROFIT)+HistoryDealGetDouble(d,DEAL_COMMISSION)+HistoryDealGetDouble(d,DEAL_SWAP)+HistoryDealGetDouble(d,DEAL_FEE);
   }
   for(int i=0;i<PositionsTotal();i++) if(PositionGetTicket(i)>0) floating+=PositionGetDouble(POSITION_PROFIT)+PositionGetDouble(POSITION_SWAP);
   string key="FX.day."+(string)pinned_login+"."+(string)midnight;
   if(!GlobalVariableCheck(key)) {GlobalVariableSet(key,AccountInfoDouble(ACCOUNT_EQUITY));GlobalVariablesFlush();}
   double pnl=MathMin(realized+floating,AccountInfoDouble(ACCOUNT_EQUITY)-GlobalVariableGet(key));
   return MathMax(0,N("capital")*N("daily_loss_pct")/100+pnl);
}
bool DailyBudget() {return DailyRemaining()>0;}
bool ExposureAllowed()
{
   if(PositionsTotal()+OrdersTotal()>=(int)N("max_positions")) return false;
   for(int i=0;i<PositionsTotal();i++) if(PositionGetTicket(i)>0 && PositionGetString(POSITION_SYMBOL)==active_symbol) return false;
   for(int i=0;i<OrdersTotal();i++) if(OrderGetTicket(i)>0 && OrderGetString(ORDER_SYMBOL)==active_symbol) return false;
   return true;
}
ENUM_ORDER_TYPE_FILLING Filling()
{
   long flags=SymbolInfoInteger(active_symbol,SYMBOL_FILLING_MODE);
   if((flags&SYMBOL_FILLING_FOK)!=0) return ORDER_FILLING_FOK;
   if((flags&SYMBOL_FILLING_IOC)!=0) return ORDER_FILLING_IOC;
   return ORDER_FILLING_RETURN;
}
double Price(const double price,const bool up)
{
   double step=SymbolInfoDouble(active_symbol,SYMBOL_TRADE_TICK_SIZE);
   return NormalizeDouble((up ? MathCeil(price/step) : MathFloor(price/step))*step,(int)SymbolInfoInteger(active_symbol,SYMBOL_DIGITS));
}
bool DefinitiveRejection(const uint code)
{
   return code==10004 || code==10006 || code==10013 || code==10014 || code==10015 || code==10016 || code==10017 || code==10018 || code==10019 || code==10020 || code==10021 || code==10022 || code==10024 || code==10026 || code==10027 || code==10030 || code==10033 || code==10034 || code==10035 || code==10038 || code==10040 || code==10042 || code==10043 || code==10044 || code==10045 || code==10046;
}
bool SendOnce(MqlTradeRequest &request,const int kind)
{
   if(!TradingAllowed() || pending_kind>0 || owner_file==INVALID_HANDLE) return false;
   MqlTradeCheckResult check={};MqlTradeResult result={};MqlTick fresh;
   if(!OrderCheck(request,check) || check.retcode!=0) {last_retcode=(int)check.retcode;Journal("check_rejected",request,result);return false;}
   if(!TickFresh(fresh)) return false;
   if(kind==1)
   {
      ReadControl();
      if(!entry_permission || TimeGMT()>lease_until || !SessionOpen() || !ExposureAllowed() || !DailyBudget()) return false;
      double scale,stop,target;bool close_existing;EventPolicy(scale,stop,target,close_existing);
      if(scale<=0 || (N("require_calendar")>0 && (!calendar_ok || TimeGMT()-calendar_query>180 || calendar_tentative))) return false;
      double point=SymbolInfoDouble(active_symbol,SYMBOL_POINT);
      if((fresh.ask-fresh.bid)/point>N("max_spread_points") || MathAbs(request.price-(request.type==ORDER_TYPE_BUY ? fresh.ask : fresh.bid))>N("max_slippage_points")*point) return false;
   }
   else if(!Owned(request.position)) return false;
   pending_kind=kind;pending_ticket=request.position;pending_sl=request.sl;pending_tp=request.tp;pending_time=TimeTradeServer();pending_comment=request.comment;
   if(!SaveState()) {pending_kind=9;reason="cannot persist intent; trading blocked";return false;}
   Journal("intent",request,result);
   // Read permission AGAIN after disk I/O; pause acknowledgment cannot occur until this handler ends.
   if(kind==1)
   {
      ReadControl(true);
      double scale,stop,target;bool close_existing;EventPolicy(scale,stop,target,close_existing);
      MqlDateTime utc;TimeToStruct(TimeGMT(),utc);
      double ratio=current_minute.baseline>0 ? current_minute.atr/current_minute.baseline : 100;
      double volatility=ratio>=N("volatility_pause_ratio") ? 0 : ratio>=N("volatility_reduce_ratio") ? .5 : 1;
      double budget=MathMin(DailyRemaining(),MathMin(N("capital"),AccountInfoDouble(ACCOUNT_EQUITY))*N("risk_per_trade_pct")/100*N("hour_"+(string)utc.hour+"_risk_scale")*scale*volatility*permission_scale);
      double point=SymbolInfoDouble(active_symbol,SYMBOL_POINT),slip=N("max_slippage_points")*point,loss=0;
      int sign=request.type==ORDER_TYPE_BUY ? 1 : -1;
      bool sized=OrderCalcProfit(request.type,request.symbol,request.volume,request.price+sign*slip,request.sl-sign*slip,loss) && loss<0 && -loss+request.volume*N("commission_per_lot")<=budget+.00001;
      double margin=0;bool margin_ok=OrderCalcMargin(request.type,request.symbol,request.volume,request.price,margin) && margin<=MathMin(AccountInfoDouble(ACCOUNT_MARGIN_FREE),MathMin(N("capital"),AccountInfoDouble(ACCOUNT_EQUITY))*N("max_margin_pct")/100);
      bool calendar_valid=N("require_calendar")==0 || (calendar_ok && TimeGMT()-calendar_query<=180 && !calendar_tentative);
      bool bars_same=iTime(active_symbol,PERIOD_M1,1)==last_bar && iTime(active_symbol,PERIOD_H1,1)==planned_h1 && iTime(active_symbol,PERIOD_D1,1)==planned_d1 && TimeTradeServer()-last_bar-60<=90;
      if(!entry_permission || TimeGMT()>lease_until || !TradingAllowed() || !TickFresh(fresh) || !SessionOpen() || !ExposureAllowed() || !calendar_valid || !bars_same || !margin_ok || !sized || (fresh.ask-fresh.bid)/point>N("max_spread_points") || MathAbs(request.price-(sign==1 ? fresh.ask : fresh.bid))>slip)
      {pending_kind=0;if(!SaveState()) pending_kind=9;return false;}
   }
   else
   {
      bool valid=TradingAllowed() && TickFresh(fresh) && Owned(request.position);
      int sign=PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY ? 1 : -1;
      double point=SymbolInfoDouble(active_symbol,SYMBOL_POINT),step=SymbolInfoDouble(active_symbol,SYMBOL_TRADE_TICK_SIZE);
      double market=sign==1 ? fresh.bid : fresh.ask;
      if(kind==2) valid=valid && MathAbs(PositionGetDouble(POSITION_VOLUME)-request.volume)<1e-8 && MathAbs(request.price-market)<=N("max_slippage_points")*point && request.type==(sign==1 ? ORDER_TYPE_SELL : ORDER_TYPE_BUY);
      if(kind==3)
      {
         double old_sl=PositionGetDouble(POSITION_SL),old_tp=PositionGetDouble(POSITION_TP),minimum=MathMax(SymbolInfoInteger(active_symbol,SYMBOL_TRADE_STOPS_LEVEL),SymbolInfoInteger(active_symbol,SYMBOL_TRADE_FREEZE_LEVEL))*point+step;
         valid=valid && request.sl>0 && (request.sl-old_sl)*sign>=step*.9 && (market-request.sl)*sign>=minimum && (request.tp-market)*sign>=minimum && (request.tp-old_tp)*sign>=-step*.1;
      }
      if(!valid) {pending_kind=0;if(!SaveState()) pending_kind=9;return false;}
   }
   bool sent=OrderSend(request,result);last_retcode=(int)result.retcode;Journal(sent ? "send_returned" : "send_failed",request,result);
   if(DefinitiveRejection(result.retcode)) {pending_kind=0;if(!SaveState()) pending_kind=9;}
   else Reconcile();
   if(pending_kind>0) {entry_permission=false;reason="uncertain execution; awaiting broker evidence";}
   return sent && result.retcode==TRADE_RETCODE_DONE;
}

void ClosePosition(const ulong ticket)
{
   if(!Owned(ticket) || pending_kind>0) return;
   MqlTick tick;if(!TickFresh(tick)) return;
   MqlTradeRequest r={};
   bool buy=PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY;
   r.action=TRADE_ACTION_DEAL;r.position=ticket;r.magic=Magic();r.symbol=active_symbol;r.volume=PositionGetDouble(POSITION_VOLUME);
   r.type=buy ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;r.price=buy ? tick.bid : tick.ask;r.deviation=(ulong)N("max_slippage_points");r.type_filling=Filling();r.comment="fx close";
   SendOnce(r,2);
}
string RiskKey(const ulong ticket) {return "FX.r."+(string)pinned_login+"."+(string)ticket;}
void ManagePositions()
{
   if(!configured) {protection="broker SL/TP only - configuration unavailable";return;}
   protection="native SL/TP + trailing";
   if(!configured || !Identity() || OwnedPositions()==0) return;
   double event_scale,event_stop,event_target;bool event_close;EventPolicy(event_scale,event_stop,event_target,event_close);
   FusionFrame frame;bool frame_ready=ComputeFrame(PERIOD_M1,frame);
   for(int i=PositionsTotal()-1;i>=0;i--)
   {
      ulong ticket=PositionGetTicket(i);if(!Owned(ticket)) continue;
      if(pending_kind>0) {protection="broker SL/TP; reconciliation pending";continue;}
      double entry=PositionGetDouble(POSITION_PRICE_OPEN),sl=PositionGetDouble(POSITION_SL),tp=PositionGetDouble(POSITION_TP);
      int sign=PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY ? 1 : -1;
      if(sl<=0) {ClosePosition(ticket);continue;} // Never silently keep unprotected owned exposure.
      if(TimeTradeServer()-(datetime)PositionGetInteger(POSITION_TIME)>=ea_hold*60 || !SessionOpen() || event_close) {ClosePosition(ticket);continue;}
      string key=RiskKey(ticket);
      if(!GlobalVariableCheck(key))
      {
         // Initial risk was recorded before entry as a fallback for a crash after fill.
         double initial=MathAbs(entry-sl);
         if(initial<=0 || (entry-sl)*sign<=0) {protection="initial risk unavailable; broker SL/TP retained";continue;}
         GlobalVariableSet(key,initial);GlobalVariablesFlush();
      }
      MqlTick tick;if(!frame_ready || !TickFresh(tick)) continue;
      double price=sign==1 ? tick.bid : tick.ask,initial=GlobalVariableGet(key),gain=(price-entry)*sign;
      if(initial<=0 || gain<=0) continue;
      double point=SymbolInfoDouble(active_symbol,SYMBOL_POINT),step=SymbolInfoDouble(active_symbol,SYMBOL_TRADE_TICK_SIZE);
      double contract=SymbolInfoDouble(active_symbol,SYMBOL_TRADE_CONTRACT_SIZE),cost=contract>0 ? N("commission_per_lot")/contract+N("max_slippage_points")*point : 0;
      double candidate=sl;
      if(gain>=ea_breakeven*initial) candidate=sign==1 ? MathMax(candidate,entry+cost) : MathMin(candidate,entry-cost);
      if(gain>=ea_trail_start*initial) candidate=sign==1 ? MathMax(candidate,price-frame.atr*ea_trail) : MathMin(candidate,price+frame.atr*ea_trail);
      candidate=Price(candidate,sign==-1);
      double minimum=MathMax(SymbolInfoInteger(active_symbol,SYMBOL_TRADE_STOPS_LEVEL),SymbolInfoInteger(active_symbol,SYMBOL_TRADE_FREEZE_LEVEL))*point+step;
      if((price-candidate)*sign<minimum || (candidate-sl)*sign<step*.9) continue;
      if(gain>=ea_trail_start*initial && (tp-price)*sign<frame.atr) tp=Price(price+sign*MathMax(frame.atr*ea_reward,minimum),sign==1);
      MqlTradeRequest r={};r.action=TRADE_ACTION_SLTP;r.position=ticket;r.symbol=active_symbol;r.magic=Magic();r.sl=candidate;r.tp=tp;r.comment="fx protect";
      SendOnce(r,3);
   }
}

void TryEntry()
{
   if(!configured || !entry_permission || pending_kind>0) return;
   reason="waiting for closed M1 signal";
   if(!TradingAllowed() || !SessionOpen()) {reason="trading permission or configured session closed";return;}
   if(N("require_calendar")>0 && (!calendar_ok || TimeGMT()-calendar_query>180 || calendar_tentative)) {reason="calendar unavailable/stale/tentative";return;}
   if(PositionsTotal()+OrdersTotal()>=(int)N("max_positions")) {reason="account exposure limit";return;}
   // One symbol, no averaging or mixing with manual exposure.
   for(int i=0;i<PositionsTotal();i++) if(PositionGetTicket(i)>0 && PositionGetString(POSITION_SYMBOL)==active_symbol) return;
   for(int i=0;i<OrdersTotal();i++) if(OrderGetTicket(i)>0 && OrderGetString(ORDER_SYMBOL)==active_symbol) return;
   if(!DailyBudget()) {reason="daily loss limit or history unavailable";return;}
   FusionFrame hour,day;
   if(!ComputeFrame(PERIOD_M1,current_minute) || !ComputeFrame(PERIOD_H1,hour) || !ComputeFrame(PERIOD_D1,day)) {reason="closed history warming";return;}
   long m=(long)(TimeTradeServer()-current_minute.bar_time-60),h=(long)(TimeTradeServer()-hour.bar_time-3600),d=(long)(TimeTradeServer()-day.bar_time-86400);
   if(m<0 || m>90 || h<0 || h>5400 || d<0 || d>129600) {reason="closed bars stale";return;}
   last_bar=current_minute.bar_time;planned_h1=hour.bar_time;planned_d1=day.bar_time;
   SignalFromFrames(current_minute,hour,day);
   if(ea_signal=="HOLD" || last_bar<=last_attempt) {reason=ea_reason;return;}
   MqlTick tick;if(!TickFresh(tick)) {reason="quote stale";return;}
   double point=SymbolInfoDouble(active_symbol,SYMBOL_POINT),step=SymbolInfoDouble(active_symbol,SYMBOL_TRADE_TICK_SIZE);
   if(point<=0 || step<=0 || (tick.ask-tick.bid)/point>N("max_spread_points")) {reason="spread or contract invalid";return;}
   MqlDateTime utc;TimeToStruct(TimeGMT(),utc);string prefix="hour_"+(string)utc.hour;
   double event_scale,event_stop,event_target;bool close_existing;EventPolicy(event_scale,event_stop,event_target,close_existing);
   double ratio=current_minute.baseline>0 ? current_minute.atr/current_minute.baseline : 100;
   double vol_scale=ratio>=N("volatility_pause_ratio") ? 0 : ratio>=N("volatility_reduce_ratio") ? .5 : 1;
   double budget=MathMin(DailyRemaining(),MathMin(N("capital"),AccountInfoDouble(ACCOUNT_EQUITY))*N("risk_per_trade_pct")/100*N(prefix+"_risk_scale")*event_scale*vol_scale*permission_scale);
   if(budget<=0) {reason="session/event/volatility budget paused";return;}
   bool buy=ea_signal=="BUY";int sign=buy ? 1 : -1;
   double entry=buy ? tick.ask : tick.bid,stop_distance=current_minute.atr*ea_stop*N(prefix+"_stop_scale")*event_stop;
   double sl=Price(entry-sign*stop_distance,!buy),tp=Price(entry+sign*stop_distance*ea_reward*N(prefix+"_target_scale")*event_target,buy);
   double reference=buy ? tick.bid : tick.ask,minimum=MathMax(SymbolInfoInteger(active_symbol,SYMBOL_TRADE_STOPS_LEVEL),SymbolInfoInteger(active_symbol,SYMBOL_TRADE_FREEZE_LEVEL))*point+step;
   if((reference-sl)*sign<minimum || (tp-reference)*sign<minimum) {reason="broker stop/freeze constraint";return;}
   double volume_min=SymbolInfoDouble(active_symbol,SYMBOL_VOLUME_MIN),volume_step=SymbolInfoDouble(active_symbol,SYMBOL_VOLUME_STEP),volume_max=SymbolInfoDouble(active_symbol,SYMBOL_VOLUME_MAX);
   double slip=N("max_slippage_points")*point,loss=0;
   if(volume_step<=0 || volume_min<=0 || !OrderCalcProfit(buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL,active_symbol,volume_min,entry+sign*slip,sl-sign*slip,loss) || loss>=0) {reason="cannot size stop risk";return;}
   double per_lot=-loss/volume_min+N("commission_per_lot"),volume=MathFloor(MathMin(volume_max,budget/per_lot)/volume_step+1e-10)*volume_step;
   double volume_limit=SymbolInfoDouble(active_symbol,SYMBOL_VOLUME_LIMIT);if(volume_limit>0) volume=MathMin(volume,MathFloor(volume_limit/volume_step)*volume_step);
   volume=NormalizeDouble(volume,8);
   if(volume<volume_min || volume*per_lot>budget+.00001) {reason="minimum lot exceeds risk budget";return;}
   double margin=0;
   if(!OrderCalcMargin(buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL,active_symbol,volume,entry,margin) || margin>MathMin(AccountInfoDouble(ACCOUNT_MARGIN_FREE),MathMin(N("capital"),AccountInfoDouble(ACCOUNT_EQUITY))*N("max_margin_pct")/100)) {reason="margin budget";return;}
   MqlTradeRequest r={};r.action=TRADE_ACTION_DEAL;r.magic=Magic();r.symbol=active_symbol;r.type=buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   r.volume=volume;r.price=entry;r.sl=sl;r.tp=tp;r.deviation=(ulong)N("max_slippage_points");r.type_filling=Filling();r.comment="fx-"+(string)last_bar;
   last_attempt=last_bar;if(!SaveState()) {pending_kind=9;reason="cannot persist consumed signal";return;}
   bool result=SendOnce(r,1);
   reason=result ? "native entry executed" : pending_kind>0 ? "entry uncertain; reconciliation required" : "entry rejected or cancelled";
   for(int i=0;i<PositionsTotal();i++)
   {
      ulong ticket=PositionGetTicket(i);
      if(Owned(ticket) && PositionGetString(POSITION_COMMENT)==r.comment)
      {
         string key=RiskKey(ticket);if(!GlobalVariableCheck(key)) {GlobalVariableSet(key,MathAbs(PositionGetDouble(POSITION_PRICE_OPEN)-sl));GlobalVariablesFlush();}
      }
   }
}

void WriteTelemetry()
{
   state=!configured ? "configuration_missing" : pending_kind>0 ? "reconciling" : entry_permission ? "enabled" : "paused";
   if(!configured) protection="broker SL/TP only - configuration unavailable";
   int f=FileOpen(folder+"executor.tmp",FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON,';',CP_UTF8);if(f==INVALID_HANDLE) return;
   FileWrite(f,"protocol","FUSION_EXEC1");FileWrite(f,"version","3.00");FileWrite(f,"login",pinned_login);FileWrite(f,"server",pinned_server);
   FileWrite(f,"instance_id",instance_id);FileWrite(f,"symbol",active_symbol);FileWrite(f,"heartbeat_utc",(long)TimeGMT());
   FileWrite(f,"config_revision",config_revision);FileWrite(f,"command_revision",command_revision);FileWrite(f,"lease_until",lease_until);
   FileWrite(f,"entries_enabled",entry_permission ? 1 : 0);FileWrite(f,"pending",pending_kind>0 ? 1 : 0);FileWrite(f,"state",state);FileWrite(f,"reason",Clean(reason));
   FileWrite(f,"signal",ea_signal);FileWrite(f,"protection",Clean(protection));FileWrite(f,"positions",configured ? OwnedPositions() : PositionsTotal());FileWrite(f,"magic",configured ? Magic() : 0);
   FileWrite(f,"calendar_fresh",calendar_ok && TimeGMT()-calendar_query<=180 ? 1 : 0);FileWrite(f,"bar_time",(long)last_bar);FileWrite(f,"last_retcode",last_retcode);FileWrite(f,"END",21);
   FileFlush(f);FileClose(f);Publish("executor.tmp","executor.csv");
   Comment("FusionExecutor 3.00 | ",state,"\n",reason,"\n",protection," | config ",config_revision);
}
int OnInit()
{
   pinned_login=AccountInfoInteger(ACCOUNT_LOGIN);pinned_server=AccountInfoString(ACCOUNT_SERVER);active_symbol=MonitorSymbol;
   if(!Identity()) {Print("FusionExecutor only supports a USD demo account.");return INIT_FAILED;}
   FolderCreate("AgentTradeFusion",FILE_COMMON);
   // No FILE_SHARE_* flags: one native executor across all local terminals/charts.
   owner_file=FileOpen(folder+"executor.owner.lock",FILE_READ|FILE_WRITE|FILE_BIN|FILE_COMMON);
   if(owner_file==INVALID_HANDLE) {Print("FusionExecutor duplicate instance rejected.");return INIT_FAILED;}
   instance_id=(string)((long)TimeLocal()*1000000+(long)(GetMicrosecondCount()%1000000));
   if(!SymbolSelect(active_symbol,true)) return INIT_FAILED;
   Packet p;if(Load("executor.active.csv",p) && ValidateConfig(p)) ApplyConfig(p);
   if(!configured) ReadConfig();
   if(configured) LoadState();
   entry_permission=false;EventSetTimer(1);WriteTelemetry();
   Print("FusionExecutor ready; entries paused on boot, native protection active.");return INIT_SUCCEEDED;
}
void Pump()
{
   if(inside_tick) return;inside_tick=true;
   if(!Identity()) {entry_permission=false;reason="account changed; remove and reattach EA";WriteTelemetry();inside_tick=false;return;}
   Reconcile();ReadControl();ReadConfig();
   if(configured) {UpdateCalendar();ManagePositions();TryEntry();}
   WriteTelemetry();inside_tick=false;
}
void OnTimer() {Pump();}
void OnTick() {ManagePositions();}
void OnTradeTransaction(const MqlTradeTransaction &transaction,const MqlTradeRequest &request,const MqlTradeResult &result) {Reconcile();}
void OnDeinit(const int why)
{
   if(owner_file==INVALID_HANDLE) return;
   EventKillTimer();entry_permission=false;reason="EA removed; broker SL/TP retained";protection="broker SL/TP only";WriteTelemetry();Comment("");
   if(owner_file!=INVALID_HANDLE) FileClose(owner_file);
}
