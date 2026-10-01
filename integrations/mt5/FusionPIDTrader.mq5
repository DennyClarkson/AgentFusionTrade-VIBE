#property copyright "AgentFusionTrade-VIBE"
#property version "1.00"
#property description "Native tick strategy / local PID exit envelope / optional Python AI manager. Demo only."
#property strict
#include "FusionPIDCore.mqh"
#include <Canvas/Canvas.mqh>

input group "启动与交易时段 / Start and session"
input long MagicNumber=610103;
input int StartHourUTC=0;                  // 0/0 = all day
input int EndHourUTC=0;
input bool BlockWeekends=true;
input bool RequireAI=false;               // Optional Python manager grants expiring entry permission
input bool AllowAIParameters=false;       // Only flat; bounded proposals; never arm via AI
input bool TesterAutoStart=true;          // Strategy Tester only; real terminal always starts paused
input bool PanelPreviewOnly=false;        // Read-only UI inspection alongside another EA; cannot trade
input group "资金和保护 / USD demo account"
input double StrategyCapital=1000;
input double RiskPercent=0.5;
input double DailyLossPercent=2;
input double MaxMarginPercent=30;
input double RoundTripCommissionPerLot=7;
input int MaxSpreadPoints=80;
input int DeviationPoints=30;
input int MaxQuoteAgeSeconds=5;
input int CooldownSeconds=300;
input int MaxHoldMinutes=240;
input group "方向与回撤入场 / Closed context, tick trigger"
input ENUM_TIMEFRAMES TrendTimeframe=PERIOD_M5;
input int FastEMA=20;
input int SlowEMA=50;
input int ATRPeriod=14;
input double TrendSeparationATR=0.20;
input int BollPeriod=20;
input double BollDeviation=2;
input int StochPeriod=14;
input double StochExtreme=20;
input int RangeLookbackDays=3;
input double RangeEdge=0.65;
input double BandProximityATR=0.12;
input double MaxPenetrationATR=0.40;
input double InitialLocalStopATR=1.5;
input double EmergencyStopATR=2.2;
input double EmergencyTargetATR=8;
input group "PID 双线 / Account USD activation, price-unit distances"
input double ActivateProfitUSD=3;
input double BrokerLockProfitUSD=3;
input double UpperDistance=1.0;
input double LowerDistance=1.0;
input double DistanceATR=0.20;             // width = max(fixed distance, closed M1 ATR * factor)
input double Kp=0.8;
input double Ki=0.04;
input double Kd=0.12;
input double DerivativeFilterSeconds=0.35;
input double MaximumLineSpeed=1.5;
input double IntegralLimit=20;
input double ResetGapSeconds=0.50;
input group "可视化 / Native chart"
input bool ShowTickPanel=true;

struct KV {string keys[];string values[];};
struct PlotPoint {double price,lower,upper,server;long msc;};
PIDSettings gains;
FusionPID pid;
CCanvas canvas;
PlotPoint points[];
string root="AgentTradeFusion\\PID\\",scope,boot,prefix="FPID.",note="paused: click START",exit_reason="",account_server;
string pending_comment="",ai_note="AI optional/off";
string arm_session="";
long account_login=0,cursor=0,pid_msc=0,ai_revision=0,ai_until=0;
int cursor_count=0,lock_handle=INVALID_HANDLE,kind=0,retcode=0;
ulong position_id=0,position_ticket=0,pending_position=0,pending_order=0,request_id=0;
datetime pending_at=0,last_entry_bar=0,last_exit=0,context_bar=0,last_export=0,last_ai_read=0;
bool armed=false,active=false,exit_latched=false,healthy=true,ai_allowed=false,dirty=false,history_dirty=true;
bool request_final=false,canvas_ready=false;
bool tick_stream_ok=false;
int session_start,session_end,panel_tab=0;
bool manual_parameters=false;
string ai_response="AI 后台未连接。可先独立运行 EA；AI 不参与 Tick 循环。";
string ui_keys[]={"activation","upper","lower","kp","ki","kd","start","end"};
double upper_width=0,lower_width=0,local_initial=0,broker_target=0,pending_sl=0;
double position_volume=0,open_cost=0,entry_volume=0;
double pending_volume=0;
double atr=0,trend_fast=0,trend_slow=0,trend_atr=0,bb_upper=0,bb_lower=0,stoch_low=0,stoch_high=0,range_low=0,range_high=0;
double previous_mid=0,daily_remaining=0,effective_upper,effective_lower,effective_stop,effective_activate;
int ma_fast=INVALID_HANDLE,ma_slow=INVALID_HANDLE,atr_handle=INVALID_HANDLE,trend_atr_handle=INVALID_HANDLE,bands_handle=INVALID_HANDLE;
ulong last_draw=0,last_protect=0;

string V(KV &p,const string k) {for(int i=0;i<ArraySize(p.keys);i++) if(p.keys[i]==k) return p.values[i];return "";}
void Put(KV &p,const string k,const string v) {int n=ArraySize(p.keys);ArrayResize(p.keys,n+1);ArrayResize(p.values,n+1);p.keys[n]=k;string safe=v;StringReplace(safe,";","，");StringReplace(safe,"\r"," ");StringReplace(safe,"\n"," ");p.values[n]=safe;}
string Num(const double x) {return DoubleToString(x,12);}
bool Read(const string file,KV &p)
{
   ArrayResize(p.keys,0);ArrayResize(p.values,0);
   int f=FileOpen(root+file,FILE_READ|FILE_CSV|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ,';',CP_UTF8);
   if(f==INVALID_HANDLE) return false;
   bool complete=false;
   while(!FileIsEnding(f) && ArraySize(p.keys)<200)
   {
      string k=FileReadString(f),v=FileReadString(f);
      if(k=="END") {complete=StringToInteger(v)==ArraySize(p.keys);break;}
      bool duplicate=false;for(int i=0;i<ArraySize(p.keys);i++) if(p.keys[i]==k) duplicate=true;
      if(k=="" || duplicate) break;
      Put(p,k,v);
   }
   FileClose(f);return complete;
}
bool Write(const string file,KV &p)
{
   ResetLastError();
   int f=FileOpen(root+file+".tmp",FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON,';',CP_UTF8);
   if(f==INVALID_HANDLE) return false;
   bool ok=true;
   for(int i=0;i<ArraySize(p.keys);i++) if(FileWrite(f,p.keys[i],p.values[i])==0) ok=false;
   if(FileWrite(f,"END",ArraySize(p.keys))==0) ok=false;
   FileFlush(f);if(GetLastError()!=0) ok=false;FileClose(f);
   return ok && FileMove(root+file+".tmp",FILE_COMMON,root+file,FILE_COMMON|FILE_REWRITE);
}
void Event(const string event,const string detail="")
{
   int f=FileOpen(root+scope+"events.csv",FILE_READ|FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ,';',CP_UTF8);
   if(f!=INVALID_HANDLE) {FileSeek(f,0,SEEK_END);FileWrite(f,(long)TimeGMT(),event,(string)position_id,detail);FileFlush(f);FileClose(f);}
   Print("FusionPID ",event,": ",detail);
}
bool Save()
{
   if(!healthy) return false; // Never turn missing/corrupt authority into a clean state.
   KV p;
   Put(p,"schema","1");Put(p,"server",account_server);Put(p,"symbol",_Symbol);Put(p,"login",(string)account_login);Put(p,"magic",(string)MagicNumber);
   Put(p,"position",(string)position_id);Put(p,"volume",Num(position_volume));Put(p,"active",(string)(int)active);
   Put(p,"centre",Num(pid.centre));Put(p,"integral",Num(pid.integral));Put(p,"derivative",Num(pid.derivative));Put(p,"error",Num(pid.previous_error));
   Put(p,"upper",Num(upper_width));Put(p,"lower",Num(lower_width));Put(p,"initial",Num(local_initial));Put(p,"pid_msc",(string)pid_msc);
   Put(p,"cursor",(string)cursor);Put(p,"cursor_count",(string)cursor_count);Put(p,"exit",(string)(int)exit_latched);Put(p,"exit_reason",exit_reason);
   Put(p,"kind",(string)kind);Put(p,"pending_position",(string)pending_position);Put(p,"pending_order",(string)pending_order);Put(p,"pending_at",(string)(long)pending_at);
   Put(p,"pending_comment",pending_comment);Put(p,"pending_sl",Num(pending_sl));Put(p,"last_entry",(string)(long)last_entry_bar);Put(p,"last_exit",(string)(long)last_exit);
   Put(p,"kp",Num(gains.kp));Put(p,"ki",Num(gains.ki));Put(p,"kd",Num(gains.kd));
   Put(p,"request_final",(string)(int)request_final);Put(p,"pending_volume",Num(pending_volume));
   if(!Write(scope+"state.csv",p)) {healthy=false;armed=false;note="STATE SAVE FAILED: broker protection retained";return false;}
   dirty=false;return true;
}
bool Identity()
{
   return AccountInfoInteger(ACCOUNT_LOGIN)==account_login && AccountInfoString(ACCOUNT_SERVER)==account_server && AccountInfoString(ACCOUNT_CURRENCY)=="USD" && (MQLInfoInteger(MQL_TESTER) || AccountInfoInteger(ACCOUNT_TRADE_MODE)==ACCOUNT_TRADE_MODE_DEMO);
}
bool CanTrade() {return !PanelPreviewOnly && Identity() && healthy && (MQLInfoInteger(MQL_TESTER) || TerminalInfoInteger(TERMINAL_CONNECTED)) && TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) && MQLInfoInteger(MQL_TRADE_ALLOWED) && AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) && AccountInfoInteger(ACCOUNT_TRADE_EXPERT);}
ulong ExecutionClock() {MqlTick q;if(MQLInfoInteger(MQL_TESTER) && SymbolInfoTick(_Symbol,q)) return (ulong)q.time_msc;return GetTickCount64();}
bool Quote(MqlTick &q)
{
   if(!SymbolInfoTick(_Symbol,q) || q.bid<=0 || q.ask<q.bid) return false;
   long age=(long)TimeTradeServer()-(long)q.time;
   return age>=-2 && age<=MaxQuoteAgeSeconds;
}
bool SelectOwned()
{
   if(PanelPreviewOnly) return false;
   position_ticket=0;int count=0;
   for(int i=0;i<PositionsTotal();i++)
   {
      ulong ticket=PositionGetTicket(i);
      if(ticket>0 && PositionGetString(POSITION_SYMBOL)==_Symbol && PositionGetInteger(POSITION_MAGIC)==MagicNumber)
      {position_ticket=ticket;count++;}
   }
   if(count>1) {healthy=false;armed=false;note="multiple owned positions: recovery required";return false;}
   return count==1 && PositionSelectByTicket(position_ticket);
}
double TickSize() {return SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_SIZE);}
double Price(const double v,const bool up) {double s=TickSize();return NormalizeDouble((up ? MathCeil(v/s) : MathFloor(v/s))*s,_Digits);}
double MinimumStop() {return MathMax(SymbolInfoInteger(_Symbol,SYMBOL_TRADE_STOPS_LEVEL),SymbolInfoInteger(_Symbol,SYMBOL_TRADE_FREEZE_LEVEL))*_Point+TickSize();}
ENUM_ORDER_TYPE_FILLING Filling()
{
   long f=SymbolInfoInteger(_Symbol,SYMBOL_FILLING_MODE);
   if((f&SYMBOL_FILLING_FOK)!=0) return ORDER_FILLING_FOK;
   if((f&SYMBOL_FILLING_IOC)!=0) return ORDER_FILLING_IOC;
   return ORDER_FILLING_RETURN;
}
bool Session()
{
   MqlDateTime utc;TimeToStruct(TimeGMT(),utc);
   if(BlockWeekends && (utc.day_of_week==0 || utc.day_of_week==6)) return false;
   return session_start==session_end || (session_start<session_end ? utc.hour>=session_start && utc.hour<session_end : utc.hour>=session_start || utc.hour<session_end);
}
bool Definitive(const uint c)
{
   return c==10004 || c==10006 || (c>=10013 && c<=10022) || c==10024 || c==10026 || c==10027 || c==10030 || c==10033 || c==10034 || c==10035 || c==10038 || (c>=10040 && c<=10046);
}
bool OwnedOrderPending()
{
   for(int i=0;i<OrdersTotal();i++) if(OrderGetTicket(i)>0 && OrderGetInteger(ORDER_MAGIC)==MagicNumber && OrderGetString(ORDER_SYMBOL)==_Symbol) return true;
   return false;
}
void ClearPending() {kind=0;request_id=0;request_final=false;pending_order=0;Save();}
void Reconcile()
{
   if(!healthy || kind==0 || !Identity() || (!MQLInfoInteger(MQL_TESTER) && !TerminalInfoInteger(TERMINAL_CONNECTED))) return;
   bool has=SelectOwned(),proof=false;ulong history_position=0;
   if(OwnedOrderPending()) return;
   if(kind==1 && has && PositionGetString(POSITION_COMMENT)==pending_comment) proof=true;
   if(kind==3 && has && (ulong)PositionGetInteger(POSITION_IDENTIFIER)==pending_position)
   {
      int s=PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY ? 1 : -1;
      proof=PositionGetDouble(POSITION_SL)>0 && s*(PositionGetDouble(POSITION_SL)-pending_sl)>=-TickSize()*0.1;
   }
   if(!proof && HistorySelect(pending_at-60,TimeTradeServer()+60))
      for(int i=0;i<HistoryDealsTotal();i++)
      {
         ulong deal=HistoryDealGetTicket(i);
         if(HistoryDealGetString(deal,DEAL_SYMBOL)!=_Symbol || HistoryDealGetInteger(deal,DEAL_MAGIC)!=MagicNumber) continue;
         long e=HistoryDealGetInteger(deal,DEAL_ENTRY);
         if(kind==1 && HistoryDealGetString(deal,DEAL_COMMENT)==pending_comment) history_position=(ulong)HistoryDealGetInteger(deal,DEAL_POSITION_ID);
         if(kind>=2 && !has && (ulong)HistoryDealGetInteger(deal,DEAL_POSITION_ID)==pending_position && (e==DEAL_ENTRY_OUT || e==DEAL_ENTRY_OUT_BY)) proof=true;
      }
   if(kind==1 && !has && history_position>0 && HistorySelectByPosition(history_position))
   {
      double entered=0,exited=0;
      for(int i=0;i<HistoryDealsTotal();i++)
      {
         ulong d=HistoryDealGetTicket(i);long e=HistoryDealGetInteger(d,DEAL_ENTRY);
         if(e==DEAL_ENTRY_IN) entered+=HistoryDealGetDouble(d,DEAL_VOLUME);
         if(e==DEAL_ENTRY_OUT || e==DEAL_ENTRY_OUT_BY) exited+=HistoryDealGetDouble(d,DEAL_VOLUME);
      }
      proof=entered>0 && exited>=entered-1e-8;
      if(proof) last_exit=TimeTradeServer(); // Preserve cooldown even if no live position was observed.
   }
   // Explicit final server reply proves a partial close completed; the remaining volume can be closed separately.
   bool final_order=request_final;
   if(pending_order>0 && HistoryOrderSelect(pending_order))
   {
      long order_state=HistoryOrderGetInteger(pending_order,ORDER_STATE);
      final_order=order_state==ORDER_STATE_FILLED || order_state==ORDER_STATE_CANCELED || order_state==ORDER_STATE_EXPIRED || order_state==ORDER_STATE_REJECTED;
   }
   if(kind==2 && final_order && has && SelectOwned() && (ulong)PositionGetInteger(POSITION_IDENTIFIER)==pending_position && PositionGetDouble(POSITION_VOLUME)<pending_volume-1e-8) proof=true;
   if(proof) {if(kind==1 && has) SyncPosition();Event("ack",IntegerToString(kind));ClearPending();history_dirty=true;}
}
bool Submit(MqlTradeRequest &r,const int action)
{
   if(kind!=0 || !CanTrade() || lock_handle==INVALID_HANDLE) return false;
   MqlTick q;if(!Quote(q)) return false;
   MqlTradeCheckResult check={};MqlTradeResult result={};
   if(!OrderCheck(r,check) || check.retcode!=0) {retcode=(int)check.retcode;note="preflight rejected "+(string)retcode;return false;}
   kind=action;pending_position=action==1 ? 0 : position_id;pending_order=0;pending_sl=r.sl;pending_at=TimeTradeServer();pending_comment=r.comment;request_final=false;
   pending_volume=r.volume;
   if(!Save()) return false;
   // Recheck AFTER durable intent: quote, identity, exposure, permissions and exact current volume.
   bool valid=CanTrade() && Quote(q);
   if(action==1)
      valid=valid && armed && Session() && (!RequireAI || (ai_allowed && TimeGMT()<ai_until)) && PositionsTotal()==0 && OrdersTotal()==0 && MathAbs(r.price-(r.type==ORDER_TYPE_BUY ? q.ask : q.bid))<=DeviationPoints*_Point;
   else
      valid=valid && SelectOwned() && (ulong)PositionGetInteger(POSITION_IDENTIFIER)==pending_position && r.position==position_ticket;
   if(action==2) valid=valid && MathAbs(r.volume-PositionGetDouble(POSITION_VOLUME))<1e-8;
   if(action==3 && valid)
   {
      int s=PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY ? 1 : -1;
      double old=PositionGetDouble(POSITION_SL),px=s==1 ? q.bid : q.ask;
      valid=old>0 && s*(r.sl-old)>TickSize()*0.5 && s*(px-r.sl)>=MinimumStop() && r.tp==PositionGetDouble(POSITION_TP);
   }
   if(!valid) {ClearPending();return false;}
   bool sent=OrderSendAsync(r,result);retcode=(int)result.retcode;request_id=result.request_id;pending_order=result.order;
   Event("request",IntegerToString(action)+" "+(string)retcode+" "+r.comment);
   if(!sent || Definitive(result.retcode))
   {
      // A timeout/connection error is NOT proof of rejection. Never retry an ambiguous request.
      if(Definitive(result.retcode)) ClearPending();
      else {armed=false;note="submission uncertain: reconciliation required";Save();}
   }
   else Save();
   return sent;
}
void LatchExit(const string why)
{
   if(exit_latched) return;
   exit_latched=true;exit_reason=why;dirty=true;Save();Event("local_exit",why);
}
void CloseOwned()
{
   // An uncertain SL-only modification cannot create exposure. Supersede it with
   // the single position-qualified exit intent; never supersede an entry/close.
   if(exit_latched && kind==3) {Event("SL_superseded_by_exit",Num(pending_sl));ClearPending();}
   if(!exit_latched || kind!=0 || !SelectOwned() || !CanTrade()) return;
   static ulong last_try=0;if(ExecutionClock()-last_try<1000) return;last_try=ExecutionClock();
   MqlTick q;if(!Quote(q)) return;
   bool buy=PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY;
   MqlTradeRequest r={};r.action=TRADE_ACTION_DEAL;r.magic=MagicNumber;r.symbol=_Symbol;r.position=position_ticket;
   r.volume=PositionGetDouble(POSITION_VOLUME);r.type=buy ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;r.price=buy ? q.bid : q.ask;
   r.deviation=DeviationPoints;r.type_filling=Filling();r.comment="pid "+StringSubstr(exit_reason,0,22);
   Submit(r,2);
}
bool Buffer(const int h,const int buffer,double &value)
{
   double a[1];if(CopyBuffer(h,buffer,1,1,a)!=1 || !MathIsValidNumber(a[0])) return false;value=a[0];return true;
}
void Context()
{
   datetime bar=iTime(_Symbol,PERIOD_M1,1);if(bar==0 || bar==context_bar) return;
   bool ok=Buffer(ma_fast,0,trend_fast) && Buffer(ma_slow,0,trend_slow) && Buffer(atr_handle,0,atr) && Buffer(trend_atr_handle,0,trend_atr) && Buffer(bands_handle,1,bb_upper) && Buffer(bands_handle,2,bb_lower);
   MqlRates recent[],days[];
   int n=CopyRates(_Symbol,PERIOD_M1,1,StochPeriod,recent),d=CopyRates(_Symbol,PERIOD_D1,1,RangeLookbackDays,days);
   if(!ok || n!=StochPeriod || d!=RangeLookbackDays || atr<=0 || trend_atr<=0) {note="waiting for closed M1/M5/D1 history";return;}
   stoch_low=recent[0].low;stoch_high=recent[0].high;range_low=days[0].low;range_high=days[0].high;
   for(int i=1;i<n;i++) {stoch_low=MathMin(stoch_low,recent[i].low);stoch_high=MathMax(stoch_high,recent[i].high);}
   for(int i=1;i<d;i++) {range_low=MathMin(range_low,days[i].low);range_high=MathMax(range_high,days[i].high);}
   context_bar=bar;
}
void Costs()
{
   if(!SelectOwned()) return;
   ulong id=(ulong)PositionGetInteger(POSITION_IDENTIFIER);
   if(!history_dirty && id==position_id && position_volume==PositionGetDouble(POSITION_VOLUME)) return;
   open_cost=0;entry_volume=0;
   if(!HistorySelectByPosition(id)) {history_dirty=true;return;}
   for(int i=0;i<HistoryDealsTotal();i++)
   {
      ulong deal=HistoryDealGetTicket(i);
      if(HistoryDealGetInteger(deal,DEAL_ENTRY)==DEAL_ENTRY_IN)
      {open_cost+=HistoryDealGetDouble(deal,DEAL_COMMISSION)+HistoryDealGetDouble(deal,DEAL_FEE);entry_volume+=HistoryDealGetDouble(deal,DEAL_VOLUME);}
   }
   history_dirty=false;SelectOwned();
}
double CostsForVolume(const double volume)
{
   double actual=entry_volume>0 ? -MathMin(0,open_cost)*volume/entry_volume : volume*RoundTripCommissionPerLot/2;
   return MathMax(volume*RoundTripCommissionPerLot,actual+volume*RoundTripCommissionPerLot/2);
}
double Profit(const double px)
{
   if(!SelectOwned()) return -1e9;
   double v=PositionGetDouble(POSITION_VOLUME),p=0;
   ENUM_ORDER_TYPE type=PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   if(!OrderCalcProfit(type,_Symbol,v,PositionGetDouble(POSITION_PRICE_OPEN),px,p)) return -1e9;
   return p+PositionGetDouble(POSITION_SWAP)-CostsForVolume(v);
}
double LockPrice()
{
   if(!SelectOwned()) return 0;
   double entry=PositionGetDouble(POSITION_PRICE_OPEN),lo=0,hi=MathMax(atr,TickSize());
   int s=PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY ? 1 : -1;
   for(int i=0;i<30 && Profit(entry+s*hi)<BrokerLockProfitUSD;i++) hi*=2;
   if(entry+s*hi<=0 || Profit(entry+s*hi)<BrokerLockProfitUSD) return 0;
   for(int i=0;i<45;i++) {double mid=(lo+hi)*0.5;if(Profit(entry+s*mid)>=BrokerLockProfitUSD) hi=mid;else lo=mid;}
   return Price(entry+s*hi,s==1);
}
void ProtectServer()
{
   if(!active || exit_latched || kind!=0 || !SelectOwned() || ExecutionClock()-last_protect<1000) return;
   last_protect=ExecutionClock();broker_target=LockPrice();
   if(broker_target<=0 || !SelectOwned()) return;
   MqlTick q;if(!Quote(q)) return;
   int s=PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY ? 1 : -1;
   double px=s==1 ? q.bid : q.ask,old=PositionGetDouble(POSITION_SL);
   if(old<=0 || s*(broker_target-old)<TickSize()*0.9 || s*(px-broker_target)<MinimumStop()) return;
   MqlTradeRequest r={};r.action=TRADE_ACTION_SLTP;r.magic=MagicNumber;r.symbol=_Symbol;r.position=position_ticket;
   r.sl=broker_target;r.tp=PositionGetDouble(POSITION_TP);r.comment="pid protect";Submit(r,3);
}
void SyncPosition()
{
   if(!healthy) return;
   if(!SelectOwned())
   {
      if(!healthy) return;
      if(position_id>0 && kind==0) {Event("flat",exit_reason=="" ? "broker/external exit; see confirmed deal reason" : exit_reason);position_id=0;active=false;exit_latched=false;position_volume=0;last_exit=TimeTradeServer();dirty=true;Save();}
      return;
   }
   ulong id=(ulong)PositionGetInteger(POSITION_IDENTIFIER);
   double volume=PositionGetDouble(POSITION_VOLUME);
   if(PositionGetDouble(POSITION_SL)<=0) {armed=false;LatchExit("missing broker SL");}
   if(id!=position_id)
   {
      if(kind!=1 || PositionGetString(POSITION_COMMENT)!=pending_comment) {healthy=false;armed=false;note="position identity differs from journal";return;}
      position_id=id;position_volume=volume;active=false;exit_latched=PositionGetDouble(POSITION_SL)<=0;pid_msc=0;history_dirty=true;
      exit_reason=exit_latched ? "missing broker SL" : "";
      int s=PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY ? 1 : -1;
      double entry=PositionGetDouble(POSITION_PRICE_OPEN),sl=PositionGetDouble(POSITION_SL);
      local_initial=atr>0 ? Price(entry-s*atr*effective_stop,s==-1) : sl;
      if(sl<=0 || local_initial<=0) LatchExit("missing protection");
      dirty=true;Save();Event("position",(string)id);
   }
   if(volume>position_volume+1e-8) {armed=false;LatchExit("unexpected volume");}
   if(kind!=2) position_volume=volume;
}
void AddPoint(const MqlTick &q,const double px)
{
   int n=ArraySize(points);if(n>=300) {for(int i=1;i<n;i++) points[i-1]=points[i];n--;ArrayResize(points,n);}
   ArrayResize(points,n+1);points[n].msc=q.time_msc;points[n].price=px;
   points[n].upper=active ? pid.centre+upper_width : 0;points[n].lower=active ? pid.centre-lower_width : (position_id>0 ? local_initial : 0);
   points[n].server=SelectOwned() ? PositionGetDouble(POSITION_SL) : 0;
}
void Process(const MqlTick &q)
{
   if(q.bid<=0 || q.ask<q.bid || !Identity() || !healthy) return;
   if(!SelectOwned()) {AddPoint(q,q.bid);return;}
   int s=PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY ? 1 : -1;
   double px=s==1 ? q.bid : q.ask;
   // Backfilled quotes cannot activate protection before the actual opening fill.
   if(q.time_msc<PositionGetInteger(POSITION_TIME_MSC)) return;
   if(!exit_latched)
   {
      if(active)
      {
         int crossed=PIDCrossing(px,pid.centre,lower_width,upper_width);
         if(crossed!=0) LatchExit(crossed<0 ? "lower touch" : "upper touch");
         else {pid.Move(px,(double)(q.time_msc-pid_msc)/1000,gains);pid_msc=q.time_msc;dirty=true;}
      }
      else if(local_initial>0 && s*(px-local_initial)<=0) LatchExit("initial stop");
      else if(!history_dirty && Profit(px)>=effective_activate)
      {
         active=true;pid.Reset(px);pid_msc=q.time_msc;upper_width=MathMax(effective_upper,atr*DistanceATR);lower_width=MathMax(effective_lower,atr*DistanceATR);
         Save();Event("pid_start",Num(px));
      }
   }
   AddPoint(q,px);
}
void ConsumeTicks()
{
   tick_stream_ok=false;
   MqlTick latest;if(!Quote(latest)) return;
   if(cursor==0)
   {
      MqlTick initial[];int count=CopyTicks(_Symbol,initial,COPY_TICKS_ALL,(ulong)latest.time_msc,4096);
      if(count<=0 || count>=4096) {note="tick bootstrap incomplete";return;}
      // SymbolInfoTick may be the LAST of several quotes at one millisecond.
      // Start at the actual end of this batch, not a fabricated ordinal of one.
      cursor=initial[count-1].time_msc;cursor_count=0;
      for(int i=count-1;i>=0 && initial[i].time_msc==cursor;i--) cursor_count++;
      Process(initial[count-1]);tick_stream_ok=true;return;
   }
   MqlTick ticks[];int n=CopyTicks(_Symbol,ticks,COPY_TICKS_ALL,(ulong)cursor,4096);
   if(n<0) {note="tick history unavailable";return;}
   int skip=cursor_count,seen=0;long start=cursor;
   for(int i=0;i<n;i++)
   {
      if(ticks[i].time_msc==start && seen++<skip) continue;
      if(ticks[i].time_msc<cursor) continue;
      Process(ticks[i]);
      if(ticks[i].time_msc==cursor) cursor_count++;else {cursor=ticks[i].time_msc;cursor_count=1;}
   }
   if(n==4096) {armed=false;if(position_id>0) LatchExit("tick backlog");note="tick backlog: entries paused";}
   else tick_stream_ok=true;
}
void DailyBudget()
{
   daily_remaining=0;datetime now=TimeTradeServer();MqlDateTime dt;TimeToStruct(now,dt);dt.hour=0;dt.min=0;dt.sec=0;
   datetime midnight=StructToTime(dt);if(!HistorySelect(midnight,now+60)) return;
   double realized=0,floating=0;
   for(int i=0;i<HistoryDealsTotal();i++)
   {
      ulong d=HistoryDealGetTicket(i);long t=HistoryDealGetInteger(d,DEAL_TYPE);
      if(t==DEAL_TYPE_BUY || t==DEAL_TYPE_SELL) realized+=HistoryDealGetDouble(d,DEAL_PROFIT)+HistoryDealGetDouble(d,DEAL_SWAP)+HistoryDealGetDouble(d,DEAL_COMMISSION)+HistoryDealGetDouble(d,DEAL_FEE);
   }
   for(int i=0;i<PositionsTotal();i++) if(PositionGetTicket(i)>0) floating+=PositionGetDouble(POSITION_PROFIT)+PositionGetDouble(POSITION_SWAP);
   daily_remaining=MathMax(0,MathMin(StrategyCapital,AccountInfoDouble(ACCOUNT_EQUITY))*DailyLossPercent/100+MathMin(0,realized+floating));
}
void Entry(const MqlTick &q)
{
   double mid=(q.bid+q.ask)*0.5,prior=previous_mid;previous_mid=mid;
   if(!armed || !healthy || !tick_stream_ok || kind!=0 || !CanTrade()) return;
   if(!Session()) {note="outside configured UTC hours";return;}
   if(RequireAI && (!ai_allowed || TimeGMT()>=ai_until)) {note="waiting for fresh AI entry permission";return;}
   if(PositionsTotal()>0 || OrdersTotal()>0) {note="exposure exists: no new entry";return;}
   if(TimeTradeServer()-last_exit<CooldownSeconds) {note="post-exit cooldown";return;}
   if(context_bar==0 || iTime(_Symbol,PERIOD_M1,1)!=context_bar || TimeTradeServer()-context_bar>150) {note="context warming/stale";return;}
   if(context_bar<=last_entry_bar || (q.ask-q.bid)/_Point>MaxSpreadPoints) return;
   int side=0;
   if(MathAbs(trend_fast-trend_slow)>=trend_atr*TrendSeparationATR) side=trend_fast>trend_slow ? 1 : -1;
   else if(range_high>range_low)
   {
      double location=(mid-range_low)/(range_high-range_low);
      if(location>=RangeEdge) side=-1;else if(location<=1-RangeEdge) side=1;
   }
   double k=stoch_high>stoch_low ? PIDClamp(100*(mid-stoch_low)/(stoch_high-stoch_low),0,100) : 50;
   bool signal=side==1 ? (mid<=bb_lower+atr*BandProximityATR || k<=StochExtreme) && mid>=bb_lower-atr*MaxPenetrationATR && prior>0 && mid>prior : side==-1 && (mid>=bb_upper-atr*BandProximityATR || k>=100-StochExtreme) && mid<=bb_upper+atr*MaxPenetrationATR && prior>0 && mid<prior;
   note=side==1 ? "long bias: waiting for pullback + tick reversal" : side==-1 ? "short bias: waiting for rally + tick reversal" : "range middle: waiting";
   if(!signal) return;
   DailyBudget();double budget=MathMin(daily_remaining,MathMin(StrategyCapital,AccountInfoDouble(ACCOUNT_EQUITY))*RiskPercent/100);
   double volume=SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MIN),entry=side==1 ? q.ask : q.bid;
   double distance=MathMax(atr*EmergencyStopATR,MinimumStop()+q.ask-q.bid);
   double sl=Price(entry-side*distance,side==-1),tp=Price(entry+side*MathMax(atr*EmergencyTargetATR,distance*2),side==1);
   double loss=0,margin=0,slip=DeviationPoints*_Point;
   ENUM_ORDER_TYPE type=side==1 ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   if(volume<=0 || !OrderCalcProfit(type,_Symbol,volume,entry+side*slip,sl-side*slip,loss) || loss>=0 || -loss+volume*RoundTripCommissionPerLot>budget || !OrderCalcMargin(type,_Symbol,volume,entry,margin) || margin>MathMin(AccountInfoDouble(ACCOUNT_MARGIN_FREE),MathMin(StrategyCapital,AccountInfoDouble(ACCOUNT_EQUITY))*MaxMarginPercent/100))
   {note="minimum lot exceeds stop-risk/margin budget";return;}
   MqlTradeRequest r={};r.action=TRADE_ACTION_DEAL;r.magic=MagicNumber;r.symbol=_Symbol;r.type=type;r.volume=volume;r.price=entry;r.sl=sl;r.tp=tp;
   r.deviation=DeviationPoints;r.type_filling=Filling();r.comment="pid-"+(string)(long)context_bar;
   last_entry_bar=context_bar;Save();Submit(r,1);
}

bool ValidNumber(KV &p,const string key,const double lo,const double hi,double &v)
{
   string raw=V(p,key);if(raw=="") return false;
   int digits=0,dots=0;
   for(int i=0;i<StringLen(raw);i++) {ushort c=StringGetCharacter(raw,i);if(c>='0' && c<='9') digits++;else if(c=='.') dots++;else if(c!='-' || i!=0) return false;}
   if(digits==0 || dots>1) return false;
   v=StringToDouble(raw);return MathIsValidNumber(v) && v>=lo && v<=hi;
}
void ReadAI()
{
   if(TimeGMT()==last_ai_read) return;last_ai_read=TimeGMT();
   KV report;if(Read(scope+"report.csv",report) && V(report,"boot")==boot) ai_response=V(report,"text");
   KV p;if(!Read(scope+"control.csv",p)) return;
   if(V(p,"boot")!=boot || V(p,"session")!=arm_session || V(p,"server")!=account_server || V(p,"symbol")!=_Symbol || StringToInteger(V(p,"login"))!=account_login || StringToInteger(V(p,"magic"))!=MagicNumber) return;
   long rev=StringToInteger(V(p,"revision")),issued=StringToInteger(V(p,"issued")),until=StringToInteger(V(p,"until"));
   if(rev<=ai_revision || issued>TimeGMT()+2 || until<=TimeGMT() || until-issued>300 || until<=issued) return;
   ai_allowed=V(p,"enabled")=="1";ai_until=until;ai_revision=rev;ai_note=StringSubstr(V(p,"reason"),0,120);
   if(!AllowAIParameters || PositionsTotal()>0 || OrdersTotal()>0 || kind!=0 || V(p,"apply")!="1") return;
   double a,b,c,u,l,s,activation;
   if(!ValidNumber(p,"kp",0,3,a) || !ValidNumber(p,"ki",0,0.5,b) || !ValidNumber(p,"kd",0,1,c) || !ValidNumber(p,"upper",0.2,10,u) || !ValidNumber(p,"lower",0.2,10,l) || !ValidNumber(p,"stop_atr",0.5,MathMin(3,EmergencyStopATR),s) || !ValidNumber(p,"activation",1,20,activation)) return;
   // Hard limits plus at most 20% relative to the manually selected input anchor (no cumulative ratchet).
   if(MathAbs(a-Kp)>MathMax(.02,Kp*.2) || MathAbs(b-Ki)>MathMax(.002,Ki*.2) || MathAbs(c-Kd)>MathMax(.01,Kd*.2) || MathAbs(u-UpperDistance)>UpperDistance*.2 || MathAbs(l-LowerDistance)>LowerDistance*.2 || MathAbs(s-InitialLocalStopATR)>InitialLocalStopATR*.2 || MathAbs(activation-ActivateProfitUSD)>ActivateProfitUSD*.2) return;
   gains.kp=a;gains.ki=b;gains.kd=c;effective_upper=u;effective_lower=l;effective_stop=s;effective_activate=activation;
   Save();Event("ai_parameters",(string)rev);
}
void Export()
{
   if(TimeGMT()==last_export) return;last_export=TimeGMT();KV p;MqlTick q;SymbolInfoTick(_Symbol,q);
   Put(p,"protocol","FUSION_PID1");Put(p,"boot",boot);Put(p,"login",(string)account_login);Put(p,"server",account_server);Put(p,"symbol",_Symbol);Put(p,"magic",(string)MagicNumber);
   Put(p,"session",arm_session);
   Put(p,"time",(string)(long)TimeGMT());Put(p,"armed",(string)(int)armed);Put(p,"position",(string)position_id);Put(p,"pending",(string)kind);Put(p,"note",note);
   Put(p,"bid",Num(q.bid));Put(p,"ask",Num(q.ask));Put(p,"atr",Num(atr));Put(p,"trend_fast",Num(trend_fast));Put(p,"trend_slow",Num(trend_slow));Put(p,"boll_upper",Num(bb_upper));Put(p,"boll_lower",Num(bb_lower));
   Put(p,"active",(string)(int)active);Put(p,"centre",Num(pid.centre));Put(p,"upper",Num(effective_upper));Put(p,"lower",Num(effective_lower));Put(p,"kp",Num(gains.kp));Put(p,"ki",Num(gains.ki));Put(p,"kd",Num(gains.kd));Put(p,"stop_atr",Num(effective_stop));Put(p,"activation",Num(effective_activate));
   Put(p,"ai_revision",(string)ai_revision);Put(p,"require_ai",(string)(int)RequireAI);Put(p,"allow_ai_parameters",(string)(int)AllowAIParameters);
   Write(scope+"telemetry.csv",p);
}
void ExportExits()
{
   static datetime last_scan=0;
   if(TimeTradeServer()-last_scan<5) return;
   if(!HistorySelect(TimeTradeServer()-7*86400,TimeTradeServer()+60)) return;
   last_scan=TimeTradeServer();
   for(int i=0;i<HistoryDealsTotal();i++)
   {
      ulong deal=HistoryDealGetTicket(i);long e=HistoryDealGetInteger(deal,DEAL_ENTRY);
      if(HistoryDealGetInteger(deal,DEAL_MAGIC)!=MagicNumber || HistoryDealGetString(deal,DEAL_SYMBOL)!=_Symbol || (e!=DEAL_ENTRY_OUT && e!=DEAL_ENTRY_OUT_BY)) continue;
      string file=scope+"deal_"+(string)deal+".csv";if(FileIsExist(root+file,FILE_COMMON)) continue;
      KV p;Put(p,"deal",(string)deal);Put(p,"position",(string)HistoryDealGetInteger(deal,DEAL_POSITION_ID));
      Put(p,"time",(string)HistoryDealGetInteger(deal,DEAL_TIME));Put(p,"price",Num(HistoryDealGetDouble(deal,DEAL_PRICE)));
      Put(p,"volume",Num(HistoryDealGetDouble(deal,DEAL_VOLUME)));Put(p,"profit",Num(HistoryDealGetDouble(deal,DEAL_PROFIT)));
      Put(p,"commission",Num(HistoryDealGetDouble(deal,DEAL_COMMISSION)));Put(p,"swap",Num(HistoryDealGetDouble(deal,DEAL_SWAP)));Put(p,"fee",Num(HistoryDealGetDouble(deal,DEAL_FEE)));
      Put(p,"reason",(string)HistoryDealGetInteger(deal,DEAL_REASON));Put(p,"comment",HistoryDealGetString(deal,DEAL_COMMENT));Write(file,p);
   }
}
void HLine(const string key,const double value,const color colour,const ENUM_LINE_STYLE style)
{
   string name=prefix+key;
   if(value<=0) {ObjectDelete(0,name);return;}
   if(ObjectFind(0,name)<0) ObjectCreate(0,name,OBJ_HLINE,0,0,value);
   ObjectSetDouble(0,name,OBJPROP_PRICE,value);ObjectSetInteger(0,name,OBJPROP_COLOR,colour);ObjectSetInteger(0,name,OBJPROP_STYLE,style);
   ObjectSetInteger(0,name,OBJPROP_SELECTABLE,false);ObjectSetString(0,name,OBJPROP_TEXT,key+" "+DoubleToString(value,_Digits));
}
void UIObject(const string name,const ENUM_OBJECT type,const int x,const int y,const int width,const int height,const string text)
{
   string key=prefix+"ui."+name;
   if(ObjectFind(0,key)<0) ObjectCreate(0,key,type,0,0,0);
   ObjectSetInteger(0,key,OBJPROP_XDISTANCE,x);ObjectSetInteger(0,key,OBJPROP_YDISTANCE,y);
   if(type!=OBJ_LABEL) {ObjectSetInteger(0,key,OBJPROP_XSIZE,width);ObjectSetInteger(0,key,OBJPROP_YSIZE,height);}
   ObjectSetInteger(0,key,OBJPROP_COLOR,clrWhite);ObjectSetInteger(0,key,OBJPROP_BGCOLOR,C'35,45,52');
   ObjectSetInteger(0,key,OBJPROP_BORDER_COLOR,C'66,83,93');ObjectSetInteger(0,key,OBJPROP_FONTSIZE,10);
   ObjectSetString(0,key,OBJPROP_FONT,"Microsoft YaHei UI");ObjectSetInteger(0,key,OBJPROP_SELECTABLE,false);
   ObjectSetInteger(0,key,OBJPROP_ZORDER,10);ObjectSetString(0,key,OBJPROP_TEXT,text=="" ? " " : text);
}
void Panel()
{
   ObjectsDeleteAll(0,prefix+"ui.");
   if(canvas_ready) {canvas.Destroy();canvas_ready=false;}
   UIObject("background",OBJ_RECTANGLE_LABEL,10,10,660,530,"");
   ObjectSetInteger(0,prefix+"ui.background",OBJPROP_BGCOLOR,C'18,24,28');ObjectSetInteger(0,prefix+"ui.background",OBJPROP_ZORDER,0);
   UIObject("title",OBJ_LABEL,26,20,0,0,"FUSION PID  /  原生 Tick 交易 · 模拟盘");
   UIObject("trade",OBJ_BUTTON,26,52,190,30,"交易与曲线");UIObject("settings",OBJ_BUTTON,226,52,190,30,"参数与时段");UIObject("ai",OBJ_BUTTON,426,52,218,30,"AI 复盘与对话");
   ObjectSetInteger(0,prefix+"ui."+(panel_tab==0 ? "trade" : panel_tab==1 ? "settings" : "ai"),OBJPROP_BGCOLOR,C'45,95,111');
   UIObject("status",OBJ_LABEL,26,94,0,0,"");
   if(panel_tab==0)
   {
      UIObject("toggle",OBJ_BUTTON,26,124,290,34,armed ? "暂停入场 / PAUSE" : "启动模拟盘 / START");
      UIObject("close",OBJ_BUTTON,328,124,316,34,"暂停并平本 EA 仓位");
      UIObject("message",OBJ_LABEL,26,450,0,0,note);
      UIObject("protection",OBJ_LABEL,26,478,0,0,"");
      UIObject("hint",OBJ_LABEL,26,506,0,0,"暂停仍保护持仓；移除 EA 后仅保留经纪商 SL/TP。");
   }
   if(panel_tab==1)
   {
      string labels[]={"PID 启动净盈利 / USD","上轨距离 / 报价单位","下轨距离 / 报价单位","Kp / 秒⁻¹","Ki / 秒⁻²","Kd","开始小时 / UTC","结束小时 / UTC"};
      double values[]={effective_activate,effective_upper,effective_lower,gains.kp,gains.ki,gains.kd,(double)session_start,(double)session_end};
      for(int i=0;i<8;i++)
      {
         int x=26+(i%2)*314,y=132+(i/2)*70;
         UIObject("label."+ui_keys[i],OBJ_LABEL,x,y,0,0,labels[i]);
         UIObject("edit."+ui_keys[i],OBJ_EDIT,x,y+26,285,28,DoubleToString(values[i],i>=6 ? 0 : 3));
      }
      UIObject("apply",OBJ_BUTTON,26,426,618,36,"暂停并保存参数（需空仓、无待确认交易）");
      UIObject("hint",OBJ_LABEL,26,478,0,0,"上下轨距离会与 ATR 下限取较大值。相同小时 = 全天。");
      UIObject("message",OBJ_LABEL,26,506,0,0,note);
   }
   if(panel_tab==2)
   {
      UIObject("question",OBJ_EDIT,26,132,618,34,"");
      UIObject("send",OBJ_BUTTON,26,178,300,32,"发送给 AI（保留上下文）");
      UIObject("review",OBJ_BUTTON,340,178,304,32,"立即复盘");
      for(int i=0;i<10;i++) UIObject("reply."+(string)i,OBJ_LABEL,26,228+i*25,0,0,"");
      UIObject("hint",OBJ_LABEL,26,506,0,0,"AI 连接 / 模型 / 思考模式在 pid-manager.json 中配置。");
   }
}
bool SaveSettings()
{
   KV p;Put(p,"activation",Num(effective_activate));Put(p,"upper",Num(effective_upper));Put(p,"lower",Num(effective_lower));
   Put(p,"kp",Num(gains.kp));Put(p,"ki",Num(gains.ki));Put(p,"kd",Num(gains.kd));Put(p,"start",(string)session_start);Put(p,"end",(string)session_end);
   return Write(scope+"settings.csv",p);
}
bool ApplySettings(KV &p)
{
   double a,u,l,kp,ki,kd,start,end;
   if(!ValidNumber(p,"activation",.1,100,a) || !ValidNumber(p,"upper",.05,100,u) || !ValidNumber(p,"lower",.05,100,l) || !ValidNumber(p,"kp",0,10,kp) || !ValidNumber(p,"ki",0,2,ki) || !ValidNumber(p,"kd",0,5,kd) || !ValidNumber(p,"start",0,23,start) || !ValidNumber(p,"end",0,23,end) || MathFloor(start)!=start || MathFloor(end)!=end) return false;
   effective_activate=a;effective_upper=u;effective_lower=l;gains.kp=kp;gains.ki=ki;gains.kd=kd;session_start=(int)start;session_end=(int)end;return true;
}
void QueueAI(const string text)
{
   KV p;Put(p,"boot",boot);Put(p,"session",arm_session);Put(p,"text",StringSubstr(text,0,2000));Put(p,"time",(string)(long)TimeGMT());
   string file=scope+"ask_"+(string)(long)TimeLocal()+"_"+(string)GetMicrosecondCount()+".csv";
   ai_response=Write(file,p) ? "已排队，等待 Python AI 后台处理……" : "请求保存失败";
}
int PY(const double value,const double lo,const double hi,const int h) {return 68+(int)((hi-value)/(hi-lo)*(h-98));}
void Draw()
{
   if(GetTickCount64()-last_draw<200) return;last_draw=GetTickCount64();
   bool has=SelectOwned();double server=has ? PositionGetDouble(POSITION_SL) : 0;
   HLine("PID upper",has && active ? pid.centre+upper_width : 0,clrOrange,STYLE_DASH);
   HLine("PID lower",has && active ? pid.centre-lower_width : 0,clrDeepSkyBlue,STYLE_DASH);
   HLine("Initial local stop",has && !active ? local_initial : 0,clrDeepSkyBlue,STYLE_DOT);
   HLine("Confirmed broker SL",server,clrTomato,STYLE_SOLID);
   ObjectSetString(0,prefix+"ui.toggle",OBJPROP_TEXT,armed ? "暂停入场 / PAUSE" : "启动模拟盘 / START");
   string status=kind>0 ? "等待交易回执 / PENDING" : exit_latched ? "触线待平仓 / EXIT" : active ? "PID 跟踪中 / TRACKING" : has ? "持仓：等待启动金额" : armed ? "已启动：等待策略信号" : "已暂停：点击 START";
   string hours=session_start==session_end ? "全天" : (string)session_start+"–"+(string)session_end+" UTC";
   ObjectSetString(0,prefix+"ui.status",OBJPROP_TEXT,status+"  |  "+_Symbol+"  |  "+hours);
   ObjectSetString(0,prefix+"ui.message",OBJPROP_TEXT,StringSubstr(note,0,76));
   string protection=has && active ? "锁盈 SL："+(broker_target>0 && server>0 && (PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY ? server>=broker_target-TickSize()*.1 : server<=broker_target+TickSize()*.1) ? "经纪商已确认" : "尚未确认 / 距离不足") : "启动阈值 $"+DoubleToString(effective_activate,2)+"  |  最小手数 / 不加仓";
   ObjectSetString(0,prefix+"ui.protection",OBJPROP_TEXT,protection);
   if(panel_tab==2) for(int i=0;i<10;i++)
   {
      string line=StringSubstr(ai_response,i*43,43);
      ObjectSetString(0,prefix+"ui.reply."+(string)i,OBJPROP_TEXT,line=="" ? " " : line);
   }
   if(!ShowTickPanel || panel_tab!=0) return;
   int width=618,height=260;
   if(!canvas_ready || canvas.Width()!=width)
   {
      if(canvas_ready) canvas.Destroy();
      canvas_ready=canvas.CreateBitmapLabel(0,0,prefix+"ticks",26,178,width,height,COLOR_FORMAT_ARGB_NORMALIZE);
      if(!canvas_ready) return;
      canvas.FontSet("Segoe UI",-90); // TextSetFont uses tenths of a point.
   }
   canvas.Erase(ColorToARGB(C'18,24,28',230));
   canvas.TextOut(12,10,"TICK / PRICE   |   native MT5 quotes",ColorToARGB(clrWhite));
   canvas.TextOut(12,30,"PRICE   |   UPPER (orange)   LOWER (blue)   BROKER SL (red)",ColorToARGB(clrSilver));
   int n=ArraySize(points);if(n<2) {canvas.Update();return;}
   double lo=points[0].price,hi=lo;
   for(int i=0;i<n;i++)
   {
      lo=MathMin(lo,points[i].price);hi=MathMax(hi,points[i].price);
      if(points[i].lower>0) lo=MathMin(lo,points[i].lower);
      if(points[i].upper>0) hi=MathMax(hi,points[i].upper);
      if(points[i].server>0) {lo=MathMin(lo,points[i].server);hi=MathMax(hi,points[i].server);}
   }
   double pad=MathMax(TickSize()*5,(hi-lo)*.12);lo-=pad;hi+=pad;
   for(int i=1;i<n;i++)
   {
      int x1=78+(i-1)*(width-94)/(n-1),x2=78+i*(width-94)/(n-1);
      canvas.Line(x1,PY(points[i-1].price,lo,hi,height),x2,PY(points[i].price,lo,hi,height),ColorToARGB(clrWhite));
      if(points[i-1].upper>0 && points[i].upper>0) canvas.Line(x1,PY(points[i-1].upper,lo,hi,height),x2,PY(points[i].upper,lo,hi,height),ColorToARGB(clrOrange));
      if(points[i-1].lower>0 && points[i].lower>0) canvas.Line(x1,PY(points[i-1].lower,lo,hi,height),x2,PY(points[i].lower,lo,hi,height),ColorToARGB(clrDeepSkyBlue));
      if(points[i-1].server>0 && points[i].server>0) canvas.Line(x1,PY(points[i-1].server,lo,hi,height),x2,PY(points[i].server,lo,hi,height),ColorToARGB(clrTomato));
   }
   canvas.TextOut(5,62,DoubleToString(hi,_Digits),ColorToARGB(clrSilver));canvas.TextOut(5,height-40,DoubleToString(lo,_Digits),ColorToARGB(clrSilver));
   canvas.TextOut(12,height-22,(string)n+" ticks / "+DoubleToString((points[n-1].msc-points[0].msc)/1000.0,1)+" sec | "+(has ? "net est $"+DoubleToString(Profit(points[n-1].price),2) : "flat"),ColorToARGB(clrSilver));
   canvas.Update();
}

bool Restore()
{
   if(!FileIsExist(root+scope+"state.csv",FILE_COMMON)) return !SelectOwned();
   KV p;if(!Read(scope+"state.csv",p) || V(p,"schema")!="1" || V(p,"server")!=account_server || V(p,"symbol")!=_Symbol || StringToInteger(V(p,"login"))!=account_login || StringToInteger(V(p,"magic"))!=MagicNumber) return false;
   double x;string numeric[]={"position","volume","active","centre","integral","derivative","error","upper","lower","initial","pid_msc","cursor","cursor_count","exit","kind","pending_position","pending_order","pending_at","pending_sl","last_entry","last_exit","kp","ki","kd","request_final","pending_volume"};
   for(int i=0;i<ArraySize(numeric);i++) if(!ValidNumber(p,numeric[i],-1e18,1e18,x)) return false;
   kind=(int)StringToInteger(V(p,"kind"));if(kind<0 || kind>3) return false;
   position_id=(ulong)StringToInteger(V(p,"position"));position_volume=StringToDouble(V(p,"volume"));active=V(p,"active")=="1";
   pid.centre=StringToDouble(V(p,"centre"));pid.integral=StringToDouble(V(p,"integral"));pid.derivative=StringToDouble(V(p,"derivative"));pid.previous_error=StringToDouble(V(p,"error"));
   upper_width=StringToDouble(V(p,"upper"));lower_width=StringToDouble(V(p,"lower"));local_initial=StringToDouble(V(p,"initial"));pid_msc=StringToInteger(V(p,"pid_msc"));
   cursor=StringToInteger(V(p,"cursor"));cursor_count=(int)StringToInteger(V(p,"cursor_count"));exit_latched=V(p,"exit")=="1";exit_reason=V(p,"exit_reason");
   pending_position=(ulong)StringToInteger(V(p,"pending_position"));pending_order=(ulong)StringToInteger(V(p,"pending_order"));pending_at=(datetime)StringToInteger(V(p,"pending_at"));pending_comment=V(p,"pending_comment");pending_sl=StringToDouble(V(p,"pending_sl"));
   last_entry_bar=(datetime)StringToInteger(V(p,"last_entry"));last_exit=(datetime)StringToInteger(V(p,"last_exit"));
   request_final=V(p,"request_final")=="1";pending_volume=StringToDouble(V(p,"pending_volume"));
   if(active && (pid.centre<=0 || upper_width<=0 || lower_width<=0 || pid_msc<=0 || position_id==0)) return false;
   if(position_id>0 && SelectOwned() && (ulong)PositionGetInteger(POSITION_IDENTIFIER)==position_id)
   {gains.kp=StringToDouble(V(p,"kp"));gains.ki=StringToDouble(V(p,"ki"));gains.kd=StringToDouble(V(p,"kd"));}
   else if(SelectOwned())
   {
      if(kind!=1 || PositionGetString(POSITION_COMMENT)!=pending_comment) return false;
   }
   else if(kind==0) {position_id=0;active=false;exit_latched=false;cursor=0;cursor_count=0;}
   return true;
}
bool InputsValid()
{
   return MagicNumber>0 && StartHourUTC>=0 && StartHourUTC<=23 && EndHourUTC>=0 && EndHourUTC<=23 && FastEMA>=2 && SlowEMA>FastEMA && ATRPeriod>=3 && BollPeriod>=5 && BollDeviation>0 && StochPeriod>=3 && StochExtreme>0 && StochExtreme<50 && RangeLookbackDays>=1 && RangeLookbackDays<=30 && RangeEdge>.5 && RangeEdge<1 && TrendSeparationATR>0 && BandProximityATR>=0 && MaxPenetrationATR>=0 && StrategyCapital>0 && RiskPercent>0 && RiskPercent<=5 && DailyLossPercent>0 && DailyLossPercent<=20 && MaxMarginPercent>0 && MaxMarginPercent<=80 && RoundTripCommissionPerLot>=0 && MaxSpreadPoints>0 && DeviationPoints>=0 && MaxQuoteAgeSeconds>0 && CooldownSeconds>=0 && MaxHoldMinutes>=1 && InitialLocalStopATR>0 && EmergencyStopATR>=InitialLocalStopATR && EmergencyTargetATR>0 && ActivateProfitUSD>0 && BrokerLockProfitUSD>=0 && UpperDistance>0 && LowerDistance>0 && DistanceATR>=0 && Kp>=0 && Ki>=0 && Kd>=0 && DerivativeFilterSeconds>0 && MaximumLineSpeed>0 && IntegralLimit>0 && ResetGapSeconds>0;
}
int OnInit()
{
   if(!PIDControllerSelfTest()) {Print("FusionPID controller self-test FAILED");return INIT_FAILED;}
   Print("FusionPID controller self-test PASSED (reference vectors / crossing / anti-windup)");
   account_login=AccountInfoInteger(ACCOUNT_LOGIN);account_server=AccountInfoString(ACCOUNT_SERVER);
   if(!Identity() || !InputsValid() || TickSize()<=0) {Print("FusionPID requires valid inputs and a USD demo account.");return INIT_PARAMETERS_INCORRECT;}
   gains.kp=Kp;gains.ki=Ki;gains.kd=Kd;gains.filter_seconds=DerivativeFilterSeconds;gains.max_speed=MaximumLineSpeed;gains.integral_limit=IntegralLimit;gains.max_dt=ResetGapSeconds;
   effective_upper=UpperDistance;effective_lower=LowerDistance;effective_stop=InitialLocalStopATR;effective_activate=ActivateProfitUSD;pid.Reset(0);
   session_start=StartHourUTC;session_end=EndHourUTC;
   FolderCreate("AgentTradeFusion",FILE_COMMON);FolderCreate("AgentTradeFusion\\PID",FILE_COMMON);
   if(MQLInfoInteger(MQL_TESTER) || PanelPreviewOnly) {root="AgentTradeFusion\\PID\\isolated_"+(string)ChartID()+"_"+(string)GetTickCount64()+"_"+(string)GetMicrosecondCount()+"\\";FolderCreate(root,FILE_COMMON);}
   else
   {
      lock_handle=FileOpen("AgentTradeFusion\\executor.owner.lock",FILE_READ|FILE_WRITE|FILE_BIN|FILE_COMMON);
      if(lock_handle==INVALID_HANDLE) {Print("Remove FusionExecutor / duplicate FusionPID from other charts first.");return INIT_FAILED;}
   }
   if(MQLInfoInteger(MQL_TESTER) || PanelPreviewOnly) lock_handle=FileOpen(root+"isolated.lock",FILE_READ|FILE_WRITE|FILE_BIN|FILE_COMMON);
   if(lock_handle==INVALID_HANDLE) {Print("FusionPID could not acquire execution lock");return INIT_FAILED;}
   uint hash=2166136261;for(int i=0;i<StringLen(account_server);i++) hash=(hash^StringGetCharacter(account_server,i))*16777619;
   scope=(string)account_login+"_"+(string)hash+"_"+_Symbol+"_"+(string)MagicNumber+"_";
   KV settings;if(!MQLInfoInteger(MQL_TESTER) && Read(scope+"settings.csv",settings)) ApplySettings(settings);
   boot=(string)(long)TimeLocal()+"-"+(string)GetMicrosecondCount();
   arm_session=boot+"-paused";
   // Tester auto-attaches every calculation indicator, including ATR subwindows.
   // Keep the chart tall enough for the native panel; values remain in telemetry.
   if(MQLInfoInteger(MQL_TESTER)) TesterHideIndicators(true);
   ma_fast=iMA(_Symbol,TrendTimeframe,FastEMA,0,MODE_EMA,PRICE_CLOSE);ma_slow=iMA(_Symbol,TrendTimeframe,SlowEMA,0,MODE_EMA,PRICE_CLOSE);
   atr_handle=iATR(_Symbol,PERIOD_M1,ATRPeriod);trend_atr_handle=iATR(_Symbol,TrendTimeframe,ATRPeriod);bands_handle=iBands(_Symbol,PERIOD_M1,BollPeriod,0,BollDeviation,PRICE_CLOSE);
   if(ma_fast==INVALID_HANDLE || ma_slow==INVALID_HANDLE || atr_handle==INVALID_HANDLE || trend_atr_handle==INVALID_HANDLE || bands_handle==INVALID_HANDLE) return INIT_FAILED;
   if(!MQLInfoInteger(MQL_TESTER) && !Restore()) {healthy=false;note="state missing/corrupt: entries blocked, broker SL retained";}
   Context();SyncPosition();Costs();
   armed=MQLInfoInteger(MQL_TESTER) && TesterAutoStart && healthy && !PanelPreviewOnly;
   Panel();EventSetMillisecondTimer(200);Export();Draw();return INIT_SUCCEEDED;
}
void OnTick()
{
   if(!Identity()) {armed=false;note="account changed: remove and reattach";return;}
   Reconcile();SyncPosition();Costs();ConsumeTicks();
   if(SelectOwned() && TimeTradeServer()-(datetime)PositionGetInteger(POSITION_TIME)>=MaxHoldMinutes*60) LatchExit("max hold");
   CloseOwned();ProtectServer();
   MqlTick q;if(Quote(q)) Entry(q);
}
void OnTimer()
{
   if(!Identity()) {armed=false;note="account changed: remove and reattach";return;}
   Reconcile();Context();SyncPosition();Costs();ConsumeTicks();ReadAI();CloseOwned();ProtectServer();
   if(dirty && healthy) Save();Export();ExportExits();Draw();
}
void OnChartEvent(const int id,const long &l,const double &d,const string &s)
{
   if(id!=CHARTEVENT_OBJECT_CLICK || StringFind(s,prefix+"ui.")!=0) return;
   ObjectSetInteger(0,s,OBJPROP_STATE,false);
   if(s==prefix+"ui.trade" || s==prefix+"ui.settings" || s==prefix+"ui.ai") {panel_tab=s==prefix+"ui.trade" ? 0 : s==prefix+"ui.settings" ? 1 : 2;Panel();}
   if(s==prefix+"ui.toggle")
   {
      ai_allowed=false;ai_until=0;arm_session=boot+"-"+(string)GetMicrosecondCount();
      if(armed) {armed=false;note="entries paused; local and broker protection continue";}
      else if(!PanelPreviewOnly && healthy && Identity() && kind==0) {armed=true;note="started by user; waiting for strategy";Event("user_start");}
      else note="cannot start: identity/state/pending request";
   }
   if(s==prefix+"ui.close") {armed=false;ai_allowed=false;ai_until=0;arm_session=boot+"-"+(string)GetMicrosecondCount();if(SelectOwned()) {LatchExit("user close");CloseOwned();}}
   if(s==prefix+"ui.apply")
   {
      armed=false;
      ai_allowed=false;ai_until=0;arm_session=boot+"-"+(string)GetMicrosecondCount();
      if(PositionsTotal()>0 || OrdersTotal()>0 || kind!=0) note="先等待空仓和交易回执，再保存参数";
      else
      {
         KV p;for(int i=0;i<ArraySize(ui_keys);i++) Put(p,ui_keys[i],ObjectGetString(0,prefix+"ui.edit."+ui_keys[i],OBJPROP_TEXT));
         if(!ApplySettings(p)) note="参数无效：金额/距离必须为正，小时为 0–23 整数";
         else if(SaveSettings() && Save()) note="参数已保存并应用；保持暂停，点击 START 开始";
         else {healthy=false;note="保存失败：已暂停";}
      }
   }
   if(s==prefix+"ui.send") QueueAI(ObjectGetString(0,prefix+"ui.question",OBJPROP_TEXT));
   if(s==prefix+"ui.review") QueueAI("请结合最近成交与当前市场做一次复盘。评估是否应暂停入场，并说明参数建议和不确定性。");
   Draw();
}
void OnTradeTransaction(const MqlTradeTransaction &t,const MqlTradeRequest &r,const MqlTradeResult &result)
{
   history_dirty=true;
   if(t.type==TRADE_TRANSACTION_REQUEST && kind>0 && result.request_id==request_id && request_id>0)
   {
      retcode=(int)result.retcode;pending_order=result.order;
      request_final=result.retcode==TRADE_RETCODE_DONE || result.retcode==TRADE_RETCODE_DONE_PARTIAL;
      if(Definitive(result.retcode)) {Event("rejected",(string)result.retcode);ClearPending();}
      else Save();
   }
   Reconcile();
}
void OnDeinit(const int why)
{
   EventKillTimer();armed=false;
   if(lock_handle!=INVALID_HANDLE) {if(healthy) Save();FileClose(lock_handle);}
   if(canvas_ready) canvas.Destroy();ObjectsDeleteAll(0,prefix);Comment("");
   if(ma_fast!=INVALID_HANDLE) IndicatorRelease(ma_fast);if(ma_slow!=INVALID_HANDLE) IndicatorRelease(ma_slow);
   if(atr_handle!=INVALID_HANDLE) IndicatorRelease(atr_handle);if(trend_atr_handle!=INVALID_HANDLE) IndicatorRelease(trend_atr_handle);if(bands_handle!=INVALID_HANDLE) IndicatorRelease(bands_handle);
}
