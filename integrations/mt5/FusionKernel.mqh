// Root-authored closed-bar M1 kernel; also used for native execution.
struct FusionFrame
{
   double fast,slow,atr,baseline,adx,slope,lower,upper,previous_lower,previous_upper,open,close,low,high;
   datetime bar_time;
};
long strategy_revision=0;
int ea_fast=20,ea_slow=50,ea_atr=14,ea_bb=20,ea_hold=45;
double ea_deviation=2,ea_separation=.2,ea_adx=20,ea_slope=.05,ea_stop=1.5,ea_reward=2,ea_trail_start=1,ea_trail=1,ea_breakeven=.8;
string ea_bias="H1",ea_signal="HOLD",ea_reason="parameters not acknowledged";

bool ComputeFrame(const ENUM_TIMEFRAMES timeframe,FusionFrame &frame)
{
   MqlRates bars[];
   ArraySetAsSeries(bars,false);
   int needed=MathMax(600,ea_slow*3);
   int count=CopyRates(active_symbol,timeframe,1,needed,bars);
   if(count<MathMax(100,ea_slow*3) || count<ea_bb+5) return false;
   double fast=bars[0].close,slow=bars[0].close,atr=0,adx=0,adtr=0,plus=0,minus=0;
   double ema_history[],atr_history[];ArrayResize(ema_history,count);ArrayResize(atr_history,count);
   double previous_lower=0,previous_upper=0,lower=0,upper=0;
   for(int i=0;i<count;i++)
   {
      if(bars[i].low<=0 || bars[i].high<MathMax(bars[i].open,bars[i].close) || bars[i].low>MathMin(bars[i].open,bars[i].close) || (i>0 && bars[i].time<=bars[i-1].time)) return false;
      fast+=2.0/(ea_fast+1)*(bars[i].close-fast);
      slow+=2.0/(ea_slow+1)*(bars[i].close-slow);
      ema_history[i]=fast;
      int p=MathMax(0,i-1);
      double tr=MathMax(bars[i].high-bars[i].low,MathMax(MathAbs(bars[i].high-bars[p].close),MathAbs(bars[i].low-bars[p].close)));
      atr+=(tr-atr)/MathMin(i+1,ea_atr);
      atr_history[i]=atr;
      int dn=MathMin(i+1,14);
      double up=bars[i].high-bars[p].high,down=bars[p].low-bars[i].low;
      adtr+=(tr-adtr)/dn;
      plus+=((up>down && up>0 ? up : 0)-plus)/dn;
      minus+=((down>up && down>0 ? down : 0)-minus)/dn;
      double di_plus=adtr>0 ? 100*plus/adtr : 0,di_minus=adtr>0 ? 100*minus/adtr : 0;
      double dx=di_plus+di_minus>0 ? 100*MathAbs(di_plus-di_minus)/(di_plus+di_minus) : 0;
      adx+=(dx-adx)/dn;
      previous_lower=lower;previous_upper=upper;
      int from=MathMax(0,i-ea_bb+1),n=i-from+1;
      double mean=0,variance=0;
      for(int j=from;j<=i;j++) mean+=bars[j].close;
      mean/=n;
      for(int j=from;j<=i;j++) variance+=MathPow(bars[j].close-mean,2);
      double sd=MathSqrt(variance/n);
      lower=mean-ea_deviation*sd;upper=mean+ea_deviation*sd;
   }
   double baseline[];ArrayResize(baseline,99);
   for(int k=0;k<99;k++) baseline[k]=atr_history[count-100+k];
   ArraySort(baseline);frame.baseline=baseline[49];
   frame.fast=fast;frame.slow=slow;frame.atr=atr;frame.adx=adx;
   frame.slope=atr>0 ? (fast-ema_history[count-6])/atr/5 : 0;
   frame.lower=lower;frame.upper=upper;frame.previous_lower=previous_lower;frame.previous_upper=previous_upper;
   frame.open=bars[count-1].open;frame.close=bars[count-1].close;frame.low=bars[count-1].low;frame.high=bars[count-1].high;frame.bar_time=bars[count-1].time;
   return true;
}

int FrameDirection(const FusionFrame &frame,const bool strict)
{
   if(frame.atr<=0 || MathAbs(frame.fast-frame.slow)/frame.atr<ea_separation) return 0;
   int direction=frame.fast>frame.slow ? 1 : -1;
   if(strict && frame.slope*direction<ea_slope && frame.adx<ea_adx) return 0;
   return direction;
}

void SignalFromFrames(const FusionFrame &minute,const FusionFrame &hour,const FusionFrame &day)
{
   ea_signal="HOLD";
   int direction=FrameDirection(minute,true);
   if(direction==0)
   {
      int h=FrameDirection(hour,false),d=FrameDirection(day,false);
      direction=ea_bias=="H1" ? h : ea_bias=="D1" ? d : h==d ? h : 0;
   }
   if(direction==1 && minute.low<=minute.previous_lower && minute.close>minute.lower && minute.close>minute.open) ea_signal="BUY";
   if(direction==-1 && minute.high>=minute.previous_upper && minute.close<minute.upper && minute.close<minute.open) ea_signal="SELL";
   ea_reason=direction==1 ? "long direction; closed lower-band recovery required" : direction==-1 ? "short direction; closed upper-band recovery required" : "uncertain higher-timeframe bias";
}

void ComputeStrategySignal()
{
   ea_signal="HOLD";
   if(strategy_revision==0) {ea_reason="awaiting strategy parameters";return;}
   FusionFrame minute,hour,day;
   if(!ComputeFrame(PERIOD_M1,minute) || !ComputeFrame(PERIOD_H1,hour) || !ComputeFrame(PERIOD_D1,day)) {ea_reason="history warming";return;}
   SignalFromFrames(minute,hour,day);
}
